# macreg

A small web utility that hands out stable MAC addresses for container IPs and remembers them.

- A **VLAN** has an IPv4 subnet, an IPv6 subnet, or both (at least one).
- An **entry** (a container) belongs to a VLAN and has an IPv4 address, an IPv6 address, or both
  (at least one). Both addresses share the entry's single MAC.
- You can add or remove either address family later, on an entry or on a VLAN.
- The MAC is generated once and never changes. Everything else about an entry is editable.
- Deleting an entry deletes its MAC from the registry, so it can be handed out again.
- Every MAC is unique across the whole registry (enforced by the app and by a UNIQUE constraint; a
  database trigger also refuses any change to a stored MAC).
- Data lives in a SQLite file on a volume.

Why: Docker macvlan assigns a new random MAC each time a container is recreated. The same IP then
appears with a different MAC, which causes stale ARP entries and UniFi "IP conflict" alerts.
Pinning a MAC per container avoids that.

## Run

```
docker compose up -d --build
```

Then open http://<host>:8080.

First add a VLAN (e.g. `vlan20` with `192.0.2.0/24` and/or `2001:db8:abcd:4::/64`). The VLAN name is
what the snippet generator uses as the Docker network name, so use the name of your macvlan network.

## Publishing the image with GitHub Actions

`.github/workflows/docker.yml` runs the tests, then builds a multi-arch (amd64 + arm64) image and
pushes it to GitHub Container Registry as `ghcr.io/psybernoid/macreg`.

- Push to `main` -> tags `latest` and `sha-<commit>`. Push a tag like `v1.2.0` -> tags `1.2.0` and `1.2`.
- Pull requests only build the image (to prove it builds); they never log in or push.
- No secrets to set up: it uses the repository's built-in `GITHUB_TOKEN`. If the push is refused,
  check Settings > Actions > General > Workflow permissions allows read/write for packages.
- A new package starts private. To pull it without logging in, open the package on GitHub
  (Packages > macreg > Package settings) and change its visibility to public.
- Then use `image: ghcr.io/<owner>/macreg:latest` in your compose file instead of `build: .`.

The image contains only the app. Your database lives in the `/data` volume and is never part of it.

## Using a MAC in a stack

Each row has a **Snippet** button that produces the compose and `docker run` lines. For example:

```yaml
services:
  jellyfin:
    mac_address: 02:a7:67:44:e8:18
    networks:
      vlan20:
        ipv4_address: 192.0.2.55
        ipv6_address: 2001:db8:abcd:4::55

networks:
  vlan20:
    external: true
```

For IPv6 addresses, the Docker network itself must be created with IPv6 enabled (`--ipv6` and an
IPv6 `--subnet`). macreg only records addresses; it doesn't touch Docker.

## Editing rules

- **Entry:** clear the IPv4 or IPv6 box and save to remove that address; fill it in to add one. At
  least one address must remain. You can also move an entry to another VLAN, as long as its
  addresses fit that VLAN's subnets (change them in the same save). The MAC stays the same.
- **VLAN:** clear a subnet box to remove it, fill it in to add one. At least one subnet must remain.
  A subnet can't be removed while entries still hold an address in it, and a subnet can't be
  changed so that an existing address falls outside it.
- An address family can only be assigned to an entry if its VLAN has a subnet of that family; in the
  add form, the box for a family the chosen VLAN lacks is disabled.

## Configuration

| Variable     | Default            | Notes |
|--------------|--------------------|-------|
| `MAC_PREFIX` | `02:a7`            | 1-5 hex octets. First octet must be unicast + locally administered (`02`, `06`, `0a`, `0e`). Avoid `02:42`, which Docker uses for its own generated MACs. The remaining octets are random. |
| `DB_PATH`    | `/data/macreg.db`  | SQLite file location. |

Changing `MAC_PREFIX` later does not alter existing MACs; it only affects new ones.

## Upgrading from the first version

The first version stored one subnet per VLAN and one IP per entry. On startup, an old database is
converted in place: each old subnet becomes a VLAN with an IPv4 or IPv6 subnet, each entry's IP goes
into the matching address column, and all ids and MACs are kept. A copy of the old database is saved
first, next to it as `macreg.db.pre-dualstack.bak`. Old IPv4 and IPv6 subnets that you'd like merged
into one VLAN can be combined afterwards: delete the entries on the v6 one, add its subnet to the v4
VLAN, then re-add the IPv6 addresses to the entries (the MAC stays on the entry you keep).

## API

JSON, under `/api`. Errors return `{"error": "..."}` with a 4xx status. (These replace the first
version's `/api/subnets` routes and single `ip` field.)

| Method | Path | Purpose |
|--------|------|---------|
| GET/POST | `/api/networks` | list / create `{name, cidr4?, cidr6?}` |
| PUT/DELETE | `/api/networks/<id>` | edit / delete. In a PUT, omit a field to leave it; send `null` or `""` to remove it |
| GET | `/api/entries` | list; filters `?ip=` (matches either family), `?mac=`, `?network_id=` |
| POST | `/api/entries` | create `{name, network_id, ip4?, ip6?, notes?}`; returns the generated `mac` |
| GET/PUT/DELETE | `/api/entries/<id>` | read / edit `{name?, network_id?, ip4?, ip6?, notes?}` / delete |
| GET | `/healthz` | health check |

Example lookup from a script: `curl -s 'http://host:8080/api/entries?ip=192.0.2.55'`.

## Other rules enforced

- Addresses must be valid, of the right family, and inside the VLAN's subnet. For IPv4 subnets of
  /30 or larger, the network and broadcast addresses are refused.
- An address can only be used once per VLAN (IPv6 is compared in normalised form, so
  `2001:db8::55` and `2001:DB8:0:0:0:0:0:55` are the same address).
- A client can't supply a MAC when creating an entry, and can't change one on edit.

## Notes

- **No authentication.** Keep it on your LAN or behind your reverse proxy's access controls.
- **Backups:** the data is one SQLite file (WAL mode). For a consistent copy while it's running:
  `docker exec macreg python -c "import sqlite3; s=sqlite3.connect('/data/macreg.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"`
  then copy `backup.db` out of the volume.
- **Bind mounts:** the container runs as UID 10001. If you replace the named volume with a bind
  mount, make the directory writable by that UID (or set `user:` in the compose file).
- Existing containers that already have a random MAC: record their IPs here and set the generated
  MAC the next time you recreate them. Nothing is read from Docker itself.

## Tests

```
pip install -r requirements.txt
python -m unittest -v test_app
```
