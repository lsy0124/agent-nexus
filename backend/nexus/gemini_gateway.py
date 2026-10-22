"""Explicit Gemini transport selection: subscription CLI first, REST only by opt-in."""
from .adapters import AgentAdapter, UnavailableAdapter
from .gemini_adapter import GeminiAdapter


class GeminiGateway(AgentAdapter):
    id, name = "gemini", "Gemini"

    def __init__(self, root, enabled=True):
        self.enabled = enabled
        try:
            from .gemini_cli_adapter import GeminiCLIAdapter
            self.cli = GeminiCLIAdapter(root, enabled)
        except Exception as exc:
            self.cli = UnavailableAdapter("gemini", "cli_load_error:" + type(exc).__name__)
        self.api = GeminiAdapter(root, enabled)
        self.mode = "cli"

    @property
    def selected(self):
        return self.cli if self.mode == "cli" else self.api

    @property
    def provider_id(self):
        return 'gemini-cli' if self.mode == 'cli' else 'gemini-api'

    @property
    def model(self):
        return getattr(self.selected, 'model', None)

    @property
    def active(self):
        return self.selected.active

    @property
    def ready(self):
        return self.selected.ready

    @property
    def verified_at(self):
        return self.selected.verified_at

    @verified_at.setter
    def verified_at(self, value):
        self.selected.verified_at = value

    @property
    def version(self):
        return self.selected.version

    @property
    def transport(self):
        return self.selected.transport

    def probe(self):
        return {**self.selected.probe(), "connection_mode": "google-subscription-cli" if self.mode == "cli" else "official-rest-api",
                "available_modes": ["google-subscription-cli", "official-rest-api"]}

    async def detect(self):
        return await self.selected.detect()

    def configure(self, *, api_key, model, approved):
        if self.active:
            raise ValueError("请先停止当前调用")
        result = self.api.configure(api_key=api_key, model=model, approved=approved)
        if hasattr(self.cli, "clear_configuration"):
            self.cli.clear_configuration()
        self.mode = "api"
        return result

    def configure_subscription(self, approved=False, model="auto"):
        if self.active:
            raise ValueError("请先停止当前调用")
        result = self.cli.configure_subscription(approved=approved, model=model)
        self.api.clear_configuration()
        self.mode = "cli"
        return result

    def clear_configuration(self):
        if hasattr(self.cli, "clear_configuration"):
            self.cli.clear_configuration()
        self.api.clear_configuration()
        return self.probe()

    async def execute(self, context):
        async for event in self.selected.execute(context):
            yield event

    async def cancel(self, task_id):
        await self.selected.cancel(task_id)
