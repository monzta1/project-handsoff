"""#163 live smoke: metrics.tonecommand.com is deployed and behind Access.

Two unauthenticated GETs, / and /api/metrics, must each answer a redirect
whose Location is on cloudflareaccess.com: the site exists and nothing is
served without the login. Rendering behind the login is the Pilot's phone
check, recorded on the run.

Set METRICS_SITE_URL to point the smoke elsewhere.
"""
import os
import sys
import urllib.error
import urllib.request

SITE = os.environ.get("METRICS_SITE_URL", "https://metrics.tonecommand.com").rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe(path: str) -> tuple[int, str]:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(f"{SITE}{path}", headers={"User-Agent": "handsoff-live-smoke"})
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "") if exc.headers else ""


def main() -> int:
    for path in ("/", "/api/metrics"):
        try:
            status, location = probe(path)
        except OSError as exc:
            print(f"LIVE_METRICS_SITE_FAILED: {SITE}{path} unreachable: {exc}")
            return 1
        if status not in (301, 302, 303, 307, 308) or "cloudflareaccess.com" not in location:
            print(f"LIVE_METRICS_SITE_FAILED: {path} answered {status} with Location {location!r}; expected a redirect to Cloudflare Access")
            return 1
        print(f"{path}: {status} to Access ({location.split('?')[0]})")
    print("LIVE_METRICS_SITE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
