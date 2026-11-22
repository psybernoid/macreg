"""macreg - a small registry that hands out stable MAC addresses for container IPs.

Model:
  * A VLAN (network) has a name and an IPv4 subnet, an IPv6 subnet, or both (at least one).
  * An entry belongs to a VLAN and has an IPv4 address, an IPv6 address, or both (at least one),
    plus ONE MAC address that serves both.
  * The MAC is generated once, when the entry is created, and never changes.
  * Deleting an entry deletes its MAC from the registry, so it can be reused.
  * MACs are locally administered unicast addresses, unique across the registry.
"""
import ipaddress
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request, send_from_directory

DB_PATH = os.environ.get("DB_PATH", "/data/macreg.db")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_prefix(prefix):
    """Validate MAC_PREFIX: 1-5 hex octets, first octet locally administered unicast."""
    octets = [o for o in prefix.lower().split(":") if o != ""]
    if not 1 <= len(octets) <= 5 or not all(re.fullmatch(r"[0-9a-f]{2}", o) for o in octets):
        raise ValueError("MAC_PREFIX must be 1-5 colon-separated hex octets, e.g. 02:a7")
    first = int(octets[0], 16)
    if first & 0x01 or not first & 0x02:
        raise ValueError(
            "MAC_PREFIX first octet must be unicast and locally administered "
            "(second hex digit 2, 6, a or e, e.g. 02, 06, 0a, 0e)"
        )
    return [int(o, 16) for o in octets]


MAC_PREFIX = parse_prefix(os.environ.get("MAC_PREFIX", "02:a7"))

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS networks (
        id    INTEGER PRIMARY KEY,
        name  TEXT NOT NULL UNIQUE,
        cidr4 TEXT UNIQUE,
        cidr6 TEXT UNIQUE,
        CHECK (cidr4 IS NOT NULL OR cidr6 IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS entries (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE RESTRICT,
        ip4        TEXT,
        ip6        TEXT,
        mac        TEXT NOT NULL UNIQUE,
        notes      TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (network_id, ip4),
        UNIQUE (network_id, ip6),
        CHECK (ip4 IS NOT NULL OR ip6 IS NOT NULL)
    )""",
    """CREATE TRIGGER IF NOT EXISTS entries_mac_immutable
       BEFORE UPDATE OF mac ON entries
       WHEN NEW.mac != OLD.mac
       BEGIN
           SELECT RAISE(ABORT, 'mac address is immutable');
       END""",
]


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect():
    # isolation_level=None -> we control transactions explicitly (BEGIN IMMEDIATE on writes),
    # so two concurrent creates can never be handed the same MAC.
    db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def migrate_v1(db):
    """v1 stored one subnet (cidr) per network row and one IP per entry. Convert in place,
    keeping every id and MAC. A backup copy of the database is written first."""
    backup_path = DB_PATH + ".pre-dualstack.bak"
    if not os.path.exists(backup_path):
        dest = sqlite3.connect(backup_path)
        db.backup(dest)
        dest.close()
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DROP TRIGGER IF EXISTS entries_mac_immutable")
        db.execute("ALTER TABLE entries RENAME TO entries_v1")
        db.execute("ALTER TABLE subnets RENAME TO subnets_v1")
        for stmt in SCHEMA:
            db.execute(stmt)
        db.execute(
            """INSERT INTO networks (id, name, cidr4, cidr6)
               SELECT id, name,
                      CASE WHEN instr(cidr, ':') = 0 THEN cidr END,
                      CASE WHEN instr(cidr, ':') > 0 THEN cidr END
               FROM subnets_v1"""
        )
        db.execute(
            """INSERT INTO entries (id, name, network_id, ip4, ip6, mac, notes, created_at, updated_at)
               SELECT id, name, subnet_id,
                      CASE WHEN instr(ip, ':') = 0 THEN ip END,
                      CASE WHEN instr(ip, ':') > 0 THEN ip END,
                      mac, notes, created_at, updated_at
               FROM entries_v1"""
        )
        db.execute("DROP TABLE entries_v1")
        db.execute("DROP TABLE subnets_v1")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = connect()
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "subnets" in tables and "networks" not in tables:
        migrate_v1(db)
    else:
        for stmt in SCHEMA:
            db.execute(stmt)
    db.close()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


FAMILIES = (4, 6)


def create_app():
    app = Flask(__name__, static_folder=None)
    init_db()

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.errorhandler(ApiError)
    def handle_api_error(e):
        return jsonify(error=e.message), e.status

    @app.errorhandler(404)
    def handle_404(_e):
        return jsonify(error="not found"), 404

    @app.errorhandler(405)
    def handle_405(_e):
        return jsonify(error="method not allowed"), 405

    # ------------------------------------------------------------------ helpers
    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("request body must be a JSON object")
        return data

    def text(data, key, required=True, maxlen=200):
        val = data.get(key)
        if val is None:
            if required:
                raise ApiError(f"'{key}' is required")
            return None
        if not isinstance(val, str):
            raise ApiError(f"'{key}' must be a string")
        val = val.strip()
        if required and not val:
            raise ApiError(f"'{key}' is required")
        if len(val) > maxlen:
            raise ApiError(f"'{key}' is too long (max {maxlen})")
        return val

    def optional_value(data, key, current=None, maxlen=64):
        """Tri-state field: key absent -> keep `current`; null or "" -> remove (None); else the string."""
        if key not in data:
            return current
        val = data[key]
        if val is None:
            return None
        if not isinstance(val, str):
            raise ApiError(f"'{key}' must be a string or null")
        val = val.strip()
        if len(val) > maxlen:
            raise ApiError(f"'{key}' is too long (max {maxlen})")
        return val or None

    def parse_network(cidr, version):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            example = "192.0.2.0/24" if version == 4 else "2001:db8:20::/64"
            raise ApiError(f"'{cidr}' is not a valid IPv{version} subnet (use CIDR, e.g. {example})")
        if net.version != version:
            raise ApiError(f"'{cidr}' is an IPv{net.version} subnet but an IPv{version} subnet is required here")
        return net

    def validate_ip(ip_text, cidr, version, network_name):
        if cidr is None:
            raise ApiError(
                f"VLAN '{network_name}' has no IPv{version} subnet; add one to the VLAN before assigning an IPv{version} address"
            )
        net = ipaddress.ip_network(cidr)
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            raise ApiError(f"'{ip_text}' is not a valid IP address")
        if ip.version != version:
            raise ApiError(f"'{ip_text}' is an IPv{ip.version} address but an IPv{version} address is required here")
        if ip not in net:
            raise ApiError(f"{ip} is not inside {net}")
        if version == 4 and net.prefixlen <= 30 and ip in (net.network_address, net.broadcast_address):
            raise ApiError(f"{ip} is the network or broadcast address of {net}")
        return str(ip)  # normalised (IPv6 compressed, lower case)

    def new_mac(db):
        """Generate a MAC not present in the registry. Call inside a write transaction."""
        for _ in range(1000):
            octets = MAC_PREFIX + [secrets.randbelow(256) for _ in range(6 - len(MAC_PREFIX))]
            mac = ":".join(f"{o:02x}" for o in octets)
            if not db.execute("SELECT 1 FROM entries WHERE mac = ?", (mac,)).fetchone():
                return mac
        raise ApiError("could not find a free MAC address; widen the MAC_PREFIX space", 500)

    ENTRY_SQL = """SELECT e.id, e.name, e.network_id, n.name AS network_name, n.cidr4, n.cidr6,
                          e.ip4, e.ip6, e.mac, e.notes, e.created_at, e.updated_at
                   FROM entries e JOIN networks n ON n.id = e.network_id"""

    def entry_row(db, entry_id):
        row = db.execute(ENTRY_SQL + " WHERE e.id = ?", (entry_id,)).fetchone()
        if row is None:
            raise ApiError("entry not found", 404)
        return row

    def get_network(db, network_id):
        if not isinstance(network_id, int) or isinstance(network_id, bool):
            raise ApiError("'network_id' must be an integer")
        row = db.execute("SELECT * FROM networks WHERE id = ?", (network_id,)).fetchone()
        if row is None:
            raise ApiError("VLAN not found", 404)
        return row

    def network_json(db, network_id):
        row = db.execute(
            """SELECT n.id, n.name, n.cidr4, n.cidr6,
                      COUNT(e.id) AS entries, COUNT(e.ip4) AS ip4_count, COUNT(e.ip6) AS ip6_count
               FROM networks n LEFT JOIN entries e ON e.network_id = n.id
               WHERE n.id = ? GROUP BY n.id""",
            (network_id,),
        ).fetchone()
        return dict(row)

    def ip_taken(db, network_id, version, ip, ignore_id=None):
        col = "ip4" if version == 4 else "ip6"  # fixed choice, never user input
        row = db.execute(
            f"SELECT name FROM entries WHERE network_id = ? AND {col} = ? AND id IS NOT ?",
            (network_id, ip, ignore_id),
        ).fetchone()
        if row:
            raise ApiError(f"{ip} is already assigned to '{row['name']}' in this VLAN", 409)

    def write_txn(fn):
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            result = fn(db)
            db.execute("COMMIT")
            return result
        except BaseException:
            db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    # ---------------------------------------------------------------- networks
    @app.get("/api/networks")
    def list_networks():
        rows = get_db().execute(
            """SELECT n.id, n.name, n.cidr4, n.cidr6,
                      COUNT(e.id) AS entries, COUNT(e.ip4) AS ip4_count, COUNT(e.ip6) AS ip6_count
               FROM networks n LEFT JOIN entries e ON e.network_id = n.id
               GROUP BY n.id ORDER BY n.name"""
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.post("/api/networks")
    def create_network():
        data = body()
        name = text(data, "name", maxlen=64)
        raw4 = optional_value(data, "cidr4")
        raw6 = optional_value(data, "cidr6")
        if raw4 is None and raw6 is None:
            raise ApiError("a VLAN needs an IPv4 subnet, an IPv6 subnet, or both")
        cidr4 = str(parse_network(raw4, 4)) if raw4 else None
        cidr6 = str(parse_network(raw6, 6)) if raw6 else None

        def work(db):
            try:
                cur = db.execute("INSERT INTO networks (name, cidr4, cidr6) VALUES (?, ?, ?)", (name, cidr4, cidr6))
            except sqlite3.IntegrityError:
                raise ApiError("a VLAN with that name, or one of those subnets, already exists", 409)
            return network_json(db, cur.lastrowid)

        return jsonify(write_txn(work)), 201

    @app.put("/api/networks/<int:network_id>")
    def update_network(network_id):
        data = body()

        def work(db):
            cur = get_network(db, network_id)
            name = text(data, "name", required=False, maxlen=64) or cur["name"]
            new = {}
            for version, key in ((4, "cidr4"), (6, "cidr6")):
                raw = optional_value(data, key, cur[key])
                if raw is None:
                    new[key] = None
                elif key in data:
                    new[key] = str(parse_network(raw, version))
                else:
                    new[key] = raw
                col = "ip4" if version == 4 else "ip6"
                if new[key] is None and cur[key] is not None:
                    n = db.execute(
                        f"SELECT COUNT(*) FROM entries WHERE network_id = ? AND {col} IS NOT NULL", (network_id,)
                    ).fetchone()[0]
                    if n:
                        raise ApiError(
                            f"{n} entr{'y has' if n == 1 else 'ies have'} an IPv{version} address in this VLAN; "
                            f"remove {'it' if n == 1 else 'them'} first",
                            409,
                        )
                elif new[key] is not None and new[key] != cur[key]:
                    net = ipaddress.ip_network(new[key])
                    for e in db.execute(
                        f"SELECT name, {col} AS ip FROM entries WHERE network_id = ? AND {col} IS NOT NULL",
                        (network_id,),
                    ):
                        if ipaddress.ip_address(e["ip"]) not in net:
                            raise ApiError(f"'{e['name']}' ({e['ip']}) would fall outside {net}", 409)
            if new["cidr4"] is None and new["cidr6"] is None:
                raise ApiError("a VLAN needs an IPv4 subnet, an IPv6 subnet, or both")
            try:
                db.execute(
                    "UPDATE networks SET name = ?, cidr4 = ?, cidr6 = ? WHERE id = ?",
                    (name, new["cidr4"], new["cidr6"], network_id),
                )
            except sqlite3.IntegrityError:
                raise ApiError("a VLAN with that name, or one of those subnets, already exists", 409)
            return network_json(db, network_id)

        return jsonify(write_txn(work))

    @app.delete("/api/networks/<int:network_id>")
    def delete_network(network_id):
        def work(db):
            get_network(db, network_id)
            n = db.execute("SELECT COUNT(*) FROM entries WHERE network_id = ?", (network_id,)).fetchone()[0]
            if n:
                raise ApiError(f"VLAN still has {n} entr{'y' if n == 1 else 'ies'}; delete or move them first", 409)
            db.execute("DELETE FROM networks WHERE id = ?", (network_id,))

        write_txn(work)
        return "", 204

    # ----------------------------------------------------------------- entries
    @app.get("/api/entries")
    def list_entries():
        sql = ENTRY_SQL
        where, args = [], []
        nid = request.args.get("network_id", type=int)
        if nid is not None:
            where.append("e.network_id = ?")
            args.append(nid)
        if request.args.get("ip"):
            try:
                ip = str(ipaddress.ip_address(request.args["ip"].strip()))
            except ValueError:
                raise ApiError("'ip' is not a valid IP address")
            where.append("(e.ip4 = ? OR e.ip6 = ?)")
            args += [ip, ip]
        if request.args.get("mac"):
            where.append("e.mac = ?")
            args.append(request.args["mac"].strip().lower().replace("-", ":"))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY n.name, e.name"
        return jsonify([dict(r) for r in get_db().execute(sql, args).fetchall()])

    @app.get("/api/entries/<int:entry_id>")
    def get_entry(entry_id):
        return jsonify(dict(entry_row(get_db(), entry_id)))

    @app.post("/api/entries")
    def create_entry():
        data = body()
        if "mac" in data:
            raise ApiError("'mac' is generated by the server and cannot be supplied")
        name = text(data, "name")
        notes = text(data, "notes", required=False, maxlen=500) or ""
        raw4 = optional_value(data, "ip4")
        raw6 = optional_value(data, "ip6")
        if raw4 is None and raw6 is None:
            raise ApiError("an entry needs an IPv4 address, an IPv6 address, or both")

        def work(db):
            net = get_network(db, data.get("network_id"))
            ip4 = validate_ip(raw4, net["cidr4"], 4, net["name"]) if raw4 else None
            ip6 = validate_ip(raw6, net["cidr6"], 6, net["name"]) if raw6 else None
            if ip4:
                ip_taken(db, net["id"], 4, ip4)
            if ip6:
                ip_taken(db, net["id"], 6, ip6)
            mac = new_mac(db)
            ts = now()
            cur = db.execute(
                "INSERT INTO entries (name, network_id, ip4, ip6, mac, notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (name, net["id"], ip4, ip6, mac, notes, ts, ts),
            )
            return dict(entry_row(db, cur.lastrowid))

        return jsonify(write_txn(work)), 201

    @app.put("/api/entries/<int:entry_id>")
    def update_entry(entry_id):
        data = body()

        def work(db):
            cur = entry_row(db, entry_id)
            if "mac" in data and str(data["mac"]).strip().lower().replace("-", ":") != cur["mac"]:
                raise ApiError("the MAC address is fixed once generated; delete the entry to release it", 400)
            name = text(data, "name", required=False) or cur["name"]
            notes = text(data, "notes", required=False, maxlen=500)
            notes = cur["notes"] if notes is None else notes
            net = get_network(db, data.get("network_id", cur["network_id"]))
            raw4 = optional_value(data, "ip4", cur["ip4"])
            raw6 = optional_value(data, "ip6", cur["ip6"])
            if raw4 is None and raw6 is None:
                raise ApiError("an entry needs an IPv4 address, an IPv6 address, or both")
            ip4 = validate_ip(raw4, net["cidr4"], 4, net["name"]) if raw4 else None
            ip6 = validate_ip(raw6, net["cidr6"], 6, net["name"]) if raw6 else None
            if ip4:
                ip_taken(db, net["id"], 4, ip4, ignore_id=entry_id)
            if ip6:
                ip_taken(db, net["id"], 6, ip6, ignore_id=entry_id)
            db.execute(
                "UPDATE entries SET name=?, network_id=?, ip4=?, ip6=?, notes=?, updated_at=? WHERE id=?",
                (name, net["id"], ip4, ip6, notes, now(), entry_id),
            )
            return dict(entry_row(db, entry_id))

        return jsonify(write_txn(work))

    @app.delete("/api/entries/<int:entry_id>")
    def delete_entry(entry_id):
        db = get_db()
        entry_row(db, entry_id)
        db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        return "", 204

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
