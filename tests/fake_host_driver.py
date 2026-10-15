"""Recording driver used by tests/test_host.py to exercise --driver loading."""


class RecordingDriver:
    name = "recording-fake"

    def capabilities(self):
        return {"host": "recording-fake", "note": "loaded through --driver MODULE:OBJECT"}

    def notify(self, root, binding, message):
        return "recorded"
