"""Pi parent notifications through the session-owned push listener."""
from ..base import ParentAdapter
from ..push import deliver

class PiParentAdapter(ParentAdapter):
    framework = "pi"

    def capabilities(self):
        return {"host": self.framework,
                "driver": "agent_link.hosts.pi.parent:PiParentAdapter",
                "notification_transport": "session_push_http",
                "receive_mode": "session_injection",
                "busy_behavior": "followup_queue",
                "wake_after_turn": "unknown", "polling": False}

    def notify(self, root, binding, message):
        return deliver(root, binding, message, role="parent")
