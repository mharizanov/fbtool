"""A stand-in for a Playwright page: serves canned payloads per URL."""

import json


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def text(self):
        return self._body


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def wheel(self, dx, dy):
        self.page.scrolls += 1
        if self.page.batches:
            for body in self.page.batches.pop(0):
                self.page.responses.append(FakeResponse(body))


class FakePage:
    """`script` maps a URL prefix to (embedded bodies, [scroll batches]).
    Serves the embedded bodies as HTML-embedded JSON on load and one batch
    per scroll, the way a feed or result list pages in more stories."""

    def __init__(self, responses, script=None, embedded=None, batches=None, final_url=None):
        self.responses = responses
        self.script = script or {}
        self.embedded = embedded or []
        self.batches = list(batches or [])
        self.scrolls = 0
        self.url = None
        self.gotos = []
        self.final_url = final_url
        self.mouse = FakeMouse(self)

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = self.final_url or url
        for prefix, (embedded, batches) in self.script.items():
            if url.startswith(prefix):
                self.embedded, self.batches = embedded, list(batches)

    def wait_for_timeout(self, ms):
        pass

    def query_selector(self, sel):
        return None

    def evaluate(self, js):
        if "querySelectorAll" in js:
            return self.embedded
        return None


def bodies_of(docs):
    return [json.dumps(d) for d in docs]
