# Remote access to Mission Control and Fleet

Both servers bind `127.0.0.1` only and have **no login**. Every mutating
request (approvals, launches, answers, settings, Fleet close and release) is
accepted only when the browser's `Origin` is the server's own loopback address
or one origin you configured explicitly. Never expose a port to the internet
directly; use one of the two setups below, where something else identifies the
person before a request reaches Handsoff.

## Configure the origins Handsoff may trust

Run dashboard, per project, in `handsoff.toml`:

```toml
[dashboard]
public_origins = ["https://mac.tailnet.ts.net"]
```

Fleet has no project, so it reads an environment variable (comma separated):

```bash
HANDSOFF_PUBLIC_ORIGINS="https://mac.tailnet.ts.net" handsoff fleet serve --port 8765
```

An origin is exactly `scheme://host[:port]`: no path, no credentials, no
query. Comparison is exact after canonicalisation (lowercase host, default
port dropped); a different port, `http` for `https`, or a longer host name is
refused. Fleet links each run dashboard on the public host with that run's
owned port when the request itself arrived on a configured origin, and keeps
the loopback link otherwise.

## Setup 1: Tailscale (recommended)

A private network; only your devices can reach the pages, and the device
identity is the authentication. Install Tailscale on this machine and on the
device you will read from, then expose each port over HTTPS on the tailnet:

```bash
tailscale serve --bg --https=443  http://127.0.0.1:8765   # Fleet
tailscale serve --bg --https=8766 http://127.0.0.1:8766   # a run dashboard
```

The origins are then `https://<machine>.<tailnet>.ts.net` for Fleet and
`https://<machine>.<tailnet>.ts.net:8766` for the run dashboard; put the Fleet
one in `HANDSOFF_PUBLIC_ORIGINS` and the run one in `[dashboard]
public_origins`. Nothing is reachable from the public internet.

## Setup 2: Cloudflare Tunnel with an Access policy

For devices without a Tailscale client. Create a tunnel to
`http://127.0.0.1:8765` (and one hostname per run port), and put a
**Cloudflare Access** policy in front of every hostname (for example, a Google
login allow-list of one address). A tunnel without Access is a public,
unauthenticated control plane for your deployments; do not do that. The
origins are the tunnel hostnames, `https://fleet.example.com` and so on.

## What stays true

- The servers still listen on loopback only; the tunnel or the tailnet proxy
  is the only thing that reaches them.
- Ownership handshakes, shutdown tokens and every refusal rule are unchanged.
- If you remove an origin from the configuration, the next request from it is
  refused; there is no session to revoke because there is no login.
