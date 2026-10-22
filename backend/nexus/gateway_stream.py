"""Bounded OpenAI-compatible SSE reader. No retries or inferred token usage."""
import json
from .bridge_protocol import BridgeError

# Transport envelopes repeat the model/id/field names for every token. They
# are not the file plan. Retain separate, finite transport and event bounds.
MAX_WIRE_BYTES = 64 * 1024 * 1024
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 200_000


async def _frames(response, stats, wire_limit, event_limit, event_count_limit):
    pending, data = bytearray(), []
    frame_bytes, frames = 0, 0

    def line(raw):
        nonlocal frame_bytes, frames, data
        frame_bytes += len(raw) + 1
        if frame_bytes > event_limit:
            raise BridgeError('stream_event_limit', f'网关 SSE 单事件超过 {event_limit} 字节；未写入文件')
        raw = raw.removesuffix(b'\r')
        if not raw:
            frames += 1
            if frames > event_count_limit:
                raise BridgeError('stream_event_limit', '网关 SSE 事件数量超过限制；未写入文件')
            result = b'\n'.join(data) if data else None
            data, frame_bytes = [], 0
            return result
        if raw.startswith(b'data:'):
            value = raw[5:]
            data.append(value[1:] if value.startswith(b' ') else value)
        return None

    async for chunk in response.aiter_bytes(chunk_size=16_384):
        stats['wire_bytes'] += len(chunk)
        if stats['wire_bytes'] > wire_limit:
            raise BridgeError('stream_transport_limit', f"网关 SSE 传输封装超过 {wire_limit} 字节（已收 {stats['wire_bytes']}）；不是文件文本上限，未写入文件")
        start = 0
        while True:
            end = chunk.find(b'\n', start)
            if end < 0:
                pending.extend(chunk[start:])
                if len(pending) + frame_bytes > event_limit:
                    raise BridgeError('stream_event_limit', f'网关 SSE 单事件超过 {event_limit} 字节；未写入文件')
                break
            pending.extend(chunk[start:end])
            result = line(bytes(pending))
            pending.clear()
            if result is not None:
                yield result
            start = end + 1
    # Accept a complete last frame without a trailing blank line. chat_events
    # still requires a real finish marker and [DONE], not merely EOF.
    if pending:
        result = line(bytes(pending))
        if result is not None:
            yield result
    if data:
        if frames >= event_count_limit:
            raise BridgeError('stream_event_limit', '网关 SSE 事件数量超过限制；未写入文件')
        yield b'\n'.join(data)


async def chat_events(response, limit=1_200_000, *, wire_limit=MAX_WIRE_BYTES,
                      event_limit=MAX_EVENT_BYTES, event_count_limit=MAX_EVENTS, stats=None):
    stats = stats if stats is not None else {}
    stats.update(wire_bytes=0, content_bytes=0, events=0)
    parts, content_bytes = [], 0
    response_id, model, finish, usage = None, None, None, None
    pieces, done = 0, False
    async for data in _frames(response, stats, wire_limit, event_limit, event_count_limit):
        stats['events'] += 1
        data = data.strip()
        if data == b'[DONE]':
            done = True
            break
        try:
            event = json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeError, RecursionError):
            raise BridgeError('protocol', '网关 SSE 数据格式无效') from None
        if not isinstance(event, dict) or event.get('error'):
            raise BridgeError('protocol', '网关 SSE 返回错误或非对象；原文未记录')
        if event.get('id') is not None:
            if response_id is not None and event['id'] != response_id:
                raise BridgeError('protocol', '同一 SSE 流出现不同请求标识')
            first_id = response_id is None
            response_id = event['id']
            if first_id:
                yield {'request_id': response_id}
        if event.get('model') is not None and event['model'] != model:
            model = event['model']
            yield {'model': model}
        choices = event.get('choices')
        if not isinstance(choices, list) or len(choices) > 1:
            raise BridgeError('protocol', '网关 SSE 候选结果格式无效')
        terminal = (choices == [] and finish is not None) or any(isinstance(c, dict) and isinstance(c.get('finish_reason'), str) and c['finish_reason'] for c in choices)
        if terminal and isinstance(event.get('usage'), dict):
            usage = event['usage']
            yield {'usage': usage}
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get('delta', {}), dict):
                raise BridgeError('protocol', '网关 SSE 增量格式无效')
            delta = choice.get('delta', {})
            if delta.get('tool_calls') or delta.get('function_call'):
                raise BridgeError('policy_violation', '网关 SSE 提出工具调用；未执行')
            addition = delta.get('content')
            if addition is not None:
                if finish is not None and addition:
                    raise BridgeError('protocol', '网关在终止标记后继续输出内容')
                if not isinstance(addition, str):
                    raise BridgeError('protocol', '网关 SSE 最终文本不是字符串')
                content_bytes += len(addition.encode('utf-8'))
                stats['content_bytes'] = content_bytes
                if content_bytes > limit:
                    raise BridgeError('response_content_limit', f'网关最终文件方案文本超过 {limit} 字节；SSE 封装另行计数，未写入文件')
                parts.append(addition)
            # Reasoning is never treated as the final file plan.
            if choice.get('finish_reason') is not None:
                if finish is not None or not isinstance(choice['finish_reason'], str):
                    raise BridgeError('protocol', '网关 SSE 重复或无效的结束标记')
                finish = choice['finish_reason']
            pieces += 1
            if pieces == 1 or pieces % 64 == 0:
                yield {'progress': pieces, 'received': stats['wire_bytes']}
    if (not done or finish is None) and finish != 'length':
        raise BridgeError('protocol', '网关 SSE 未完整结束；未写入文件，不自动重试')
    document = {'id': response_id, 'model': model,
                'choices': [{'message': {'content': ''.join(parts)}, 'finish_reason': finish}]}
    if usage is not None:
        document['usage'] = usage
    yield {'document': document, 'received': stats['wire_bytes']}
