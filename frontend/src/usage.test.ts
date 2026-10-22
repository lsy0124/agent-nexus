import { describe, expect, it } from 'vitest';
import { cost, percent, tokens } from './api';

describe('用量与费用的前端展示规则', () => {
  it('未上报的 Token 显示为「未报告」而不是 0', () => {
    // A provider that returned no usage must never render as a confident zero.
    expect(tokens(null)).toBe('未报告');
    expect(tokens(undefined)).toBe('未报告');
    expect(tokens(0)).toBe('0');
    expect(tokens(12345)).toBe('12,345');
  });

  it('费用一律标注为估算，并区分不可估算', () => {
    expect(cost(null, 'USD')).toBe('不可估算');
    expect(cost(0.0123, 'USD')).toContain('估算');
    expect(cost(0.0123, 'USD')).toContain('USD');
    // Credits are a plan unit, not money, and are labelled as such.
    expect(cost(1536, 'credits')).toContain('credits');
    expect(cost(1536, 'credits')).toContain('估算');
  });

  it('没有调用时成功率显示为占位符而不是 100%', () => {
    expect(percent(null)).toBe('—');
    expect(percent(undefined)).toBe('—');
    expect(percent(0)).toBe('0.0%');
    expect(percent(0.5)).toBe('50.0%');
  });
});
