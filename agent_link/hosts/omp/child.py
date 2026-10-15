"""OMP child delivery uses its session-owned push listener."""
from ..base import ChildAdapter
from ..push import deliver

class OmpChildAdapter(ChildAdapter):
    framework = "omp"

    def capabilities(self):
        return {"child": "omp", "delivery_transport": "session_push_http",
                "wake_child": "native_injection", "polling": False,
                "processed_confirmation": "answer_parent persisted state"}

    def deliver(self, root, binding, message):
        return deliver(root, binding, message, role="child")
