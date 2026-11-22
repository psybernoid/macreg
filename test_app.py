import importlib
import os
import sqlite3
import tempfile
import unittest

V4 = "192.0.2.0/24"
V6 = "2001:db8:abcd:4::/64"


def load_app(db_path):
    os.environ["DB_PATH"] = db_path
    os.environ["MAC_PREFIX"] = "02:a7"
    import app as appmod
    return importlib.reload(appmod)


class MacregTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.appmod = load_app(self.db_path)
        self.client = self.appmod.app.test_client()
        self.dual = self.mk_net("vlan20", V4, V6)

    def tearDown(self):
        self.tmp.cleanup()

    def mk_net(self, name, cidr4=None, cidr6=None):
        r = self.client.post("/api/networks", json={"name": name, "cidr4": cidr4, "cidr6": cidr6})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["id"]

    def mk(self, name, net, ip4=None, ip6=None, **kw):
        return self.client.post("/api/entries", json={"name": name, "network_id": net, "ip4": ip4, "ip6": ip6, **kw})

    def put_entry(self, eid, **kw):
        return self.client.put(f"/api/entries/{eid}", json=kw)

    def put_net(self, nid, **kw):
        return self.client.put(f"/api/networks/{nid}", json=kw)

    # ---------------------------------------------------------------- networks
    def test_network_needs_at_least_one_subnet(self):
        r = self.client.post("/api/networks", json={"name": "x"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/networks", json={"name": "x", "cidr4": "", "cidr6": None})
        self.assertEqual(r.status_code, 400)

    def test_network_can_be_v4_only_v6_only_or_both(self):
        a = self.client.post("/api/networks", json={"name": "a", "cidr4": "10.1.0.0/24"}).get_json()
        b = self.client.post("/api/networks", json={"name": "b", "cidr6": "fd00:b::/64"}).get_json()
        self.assertEqual((a["cidr4"], a["cidr6"]), ("10.1.0.0/24", None))
        self.assertEqual((b["cidr4"], b["cidr6"]), (None, "fd00:b::/64"))

    def test_network_subnet_family_enforced(self):
        self.assertEqual(self.client.post("/api/networks", json={"name": "x", "cidr4": "fd00::/64"}).status_code, 400)
        self.assertEqual(self.client.post("/api/networks", json={"name": "y", "cidr6": "10.0.0.0/24"}).status_code, 400)
        self.assertEqual(self.client.post("/api/networks", json={"name": "z", "cidr4": "nope"}).status_code, 400)

    def test_network_host_bits_normalised_and_duplicates_rejected(self):
        r = self.client.post("/api/networks", json={"name": "n", "cidr4": "10.55.0.7/24"})
        self.assertEqual(r.get_json()["cidr4"], "10.55.0.0/24")
        self.assertEqual(self.client.post("/api/networks", json={"name": "n", "cidr4": "10.56.0.0/24"}).status_code, 409)
        self.assertEqual(self.client.post("/api/networks", json={"name": "m", "cidr4": "10.55.0.0/24"}).status_code, 409)

    def test_add_ipv6_subnet_to_v4_only_network_later(self):
        nid = self.mk_net("late", cidr4="10.9.0.0/24")
        r = self.put_net(nid, cidr6="fd00:9::/64")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["cidr6"], "fd00:9::/64")
        self.assertEqual(r.get_json()["cidr4"], "10.9.0.0/24")  # untouched

    def test_remove_one_subnet_from_dual_network(self):
        r = self.put_net(self.dual, cidr6=None)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(r.get_json()["cidr6"])
        self.assertEqual(r.get_json()["cidr4"], V4)

    def test_cannot_remove_last_subnet(self):
        nid = self.mk_net("one", cidr4="10.8.0.0/24")
        self.assertEqual(self.put_net(nid, cidr4=None).status_code, 400)
        self.assertEqual(self.put_net(self.dual, cidr4=None, cidr6="").status_code, 400)

    def test_cannot_remove_subnet_still_used_by_entries(self):
        self.mk("web", self.dual, ip6="2001:db8:abcd:4::10")
        r = self.put_net(self.dual, cidr6=None)
        self.assertEqual(r.status_code, 409)
        self.assertIn("IPv6", r.get_json()["error"])
        # v4 side has no entries using it, so that can go
        self.assertEqual(self.put_net(self.dual, cidr4=None).status_code, 200)

    def test_subnet_change_must_still_contain_addresses(self):
        self.mk("a", self.dual, ip4="192.0.2.5")
        self.assertEqual(self.put_net(self.dual, cidr4="203.0.113.0/24").status_code, 409)
        self.assertEqual(self.put_net(self.dual, cidr4="192.0.0.0/16").status_code, 200)

    def test_network_delete_blocked_while_in_use(self):
        e = self.mk("a", self.dual, ip4="192.0.2.5").get_json()
        self.assertEqual(self.client.delete(f"/api/networks/{self.dual}").status_code, 409)
        self.client.delete(f"/api/entries/{e['id']}")
        self.assertEqual(self.client.delete(f"/api/networks/{self.dual}").status_code, 204)

    # ----------------------------------------------------------------- entries
    def test_entry_dual_stack_shares_one_mac(self):
        r = self.mk("jellyfin", self.dual, "192.0.2.55", "2001:db8:abcd:4::55")
        self.assertEqual(r.status_code, 201, r.get_json())
        e = r.get_json()
        self.assertEqual((e["ip4"], e["ip6"]), ("192.0.2.55", "2001:db8:abcd:4::55"))
        self.assertRegex(e["mac"], r"^02:a7(:[0-9a-f]{2}){4}$")
        first = int(e["mac"][:2], 16)
        self.assertTrue(first & 2 and not first & 1)

    def test_entry_v4_only_and_v6_only(self):
        a = self.mk("a", self.dual, ip4="192.0.2.6").get_json()
        b = self.mk("b", self.dual, ip6="2001:db8:abcd:4::6").get_json()
        self.assertEqual((a["ip4"], a["ip6"]), ("192.0.2.6", None))
        self.assertEqual((b["ip4"], b["ip6"]), (None, "2001:db8:abcd:4::6"))

    def test_entry_needs_at_least_one_address(self):
        self.assertEqual(self.mk("a", self.dual).status_code, 400)
        self.assertEqual(self.mk("a", self.dual, "", "  ").status_code, 400)

    def test_entry_family_must_exist_on_network(self):
        v4only = self.mk_net("v4only", cidr4="10.7.0.0/24")
        r = self.mk("a", v4only, ip6="fd00:7::5")
        self.assertEqual(r.status_code, 400)
        self.assertIn("no IPv6 subnet", r.get_json()["error"])

    def test_ip_validation(self):
        self.assertEqual(self.mk("a", self.dual, ip4="10.99.0.5").status_code, 400)           # outside subnet
        self.assertEqual(self.mk("a", self.dual, ip4="2001:db8:abcd:4::5").status_code, 400)  # v6 in v4 box
        self.assertEqual(self.mk("a", self.dual, ip6="192.0.2.5").status_code, 400)           # v4 in v6 box
        self.assertEqual(self.mk("a", self.dual, ip4="192.0.2.0").status_code, 400)           # network addr
        self.assertEqual(self.mk("a", self.dual, ip4="192.0.2.255").status_code, 400)         # broadcast
        self.assertEqual(self.mk("a", self.dual, ip4="not-an-ip").status_code, 400)

    def test_ipv6_normalised_and_duplicates_detected(self):
        e = self.mk("a", self.dual, ip6="2001:DB8:ABCD:4:0:0:0:55").get_json()
        self.assertEqual(e["ip6"], "2001:db8:abcd:4::55")
        r = self.mk("b", self.dual, ip6="2001:db8:abcd:4::0055")
        self.assertEqual(r.status_code, 409)

    def test_duplicate_ip_in_network_rejected_per_family(self):
        self.mk("a", self.dual, "192.0.2.5", "2001:db8:abcd:4::5")
        self.assertEqual(self.mk("b", self.dual, ip4="192.0.2.5").status_code, 409)
        self.assertEqual(self.mk("c", self.dual, ip6="2001:db8:abcd:4::5").status_code, 409)
        # the failed attempts must not have consumed anything
        self.assertEqual(len(self.client.get("/api/entries").get_json()), 1)

    def test_client_cannot_supply_mac(self):
        self.assertEqual(self.mk("a", self.dual, "192.0.2.5", mac="02:00:00:00:00:01").status_code, 400)

    # -------------------------------------------------- editing (the key rules)
    def test_add_ipv6_to_existing_v4_entry_keeps_mac(self):
        e = self.mk("jellyfin", self.dual, ip4="192.0.2.55").get_json()
        r = self.put_entry(e["id"], ip6="2001:db8:abcd:4::55")
        self.assertEqual(r.status_code, 200, r.get_json())
        out = r.get_json()
        self.assertEqual(out["mac"], e["mac"])
        self.assertEqual((out["ip4"], out["ip6"]), ("192.0.2.55", "2001:db8:abcd:4::55"))

    def test_remove_one_address_keeps_the_other_and_the_mac(self):
        e = self.mk("a", self.dual, "192.0.2.5", "2001:db8:abcd:4::5").get_json()
        r = self.put_entry(e["id"], ip4=None)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((r.get_json()["ip4"], r.get_json()["ip6"]), (None, "2001:db8:abcd:4::5"))
        self.assertEqual(r.get_json()["mac"], e["mac"])
        r = self.put_entry(e["id"], ip4="192.0.2.5", ip6="")  # swap back to the other family
        self.assertEqual((r.get_json()["ip4"], r.get_json()["ip6"]), ("192.0.2.5", None))

    def test_cannot_remove_the_last_address(self):
        e = self.mk("a", self.dual, ip4="192.0.2.5").get_json()
        self.assertEqual(self.put_entry(e["id"], ip4=None).status_code, 400)
        e2 = self.mk("b", self.dual, "192.0.2.6", "2001:db8:abcd:4::6").get_json()
        self.assertEqual(self.put_entry(e2["id"], ip4="", ip6=None).status_code, 400)

    def test_absent_fields_are_left_alone(self):
        e = self.mk("a", self.dual, "192.0.2.5", "2001:db8:abcd:4::5", notes="hi").get_json()
        r = self.put_entry(e["id"], name="renamed")
        out = r.get_json()
        self.assertEqual((out["name"], out["ip4"], out["ip6"], out["notes"]),
                         ("renamed", "192.0.2.5", "2001:db8:abcd:4::5", "hi"))

    def test_move_entry_to_another_vlan_keeps_mac(self):
        other = self.mk_net("lan", cidr4="198.51.100.0/24")
        e = self.mk("jellyfin", self.dual, ip4="192.0.2.55").get_json()
        r = self.put_entry(e["id"], network_id=other, ip4="198.51.100.55")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["mac"], e["mac"])
        self.assertEqual(r.get_json()["network_name"], "lan")

    def test_move_fails_if_addresses_dont_fit_new_vlan(self):
        other = self.mk_net("lan", cidr4="198.51.100.0/24")
        e = self.mk("a", self.dual, "192.0.2.5", "2001:db8:abcd:4::5").get_json()
        self.assertEqual(self.put_entry(e["id"], network_id=other).status_code, 400)

    def test_edit_to_taken_ip_rejected_same_ip_ok(self):
        self.mk("a", self.dual, ip4="192.0.2.5")
        b = self.mk("b", self.dual, ip4="192.0.2.6").get_json()
        self.assertEqual(self.put_entry(b["id"], ip4="192.0.2.5").status_code, 409)
        self.assertEqual(self.put_entry(b["id"], ip4="192.0.2.6", name="b2").status_code, 200)

    def test_mac_cannot_be_changed(self):
        e = self.mk("a", self.dual, ip4="192.0.2.5").get_json()
        self.assertEqual(self.put_entry(e["id"], mac="02:00:00:00:00:99").status_code, 400)
        ok = self.put_entry(e["id"], mac=e["mac"].upper(), notes="x")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["mac"], e["mac"])

    def test_db_trigger_blocks_mac_update(self):
        e = self.mk("a", self.dual, ip4="192.0.2.5").get_json()
        db = sqlite3.connect(self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE entries SET mac = '02:00:00:00:00:99' WHERE id = ?", (e["id"],))
        db.close()

    def test_db_requires_an_address_and_a_subnet(self):
        db = sqlite3.connect(self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO networks (name) VALUES ('bad')")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO entries (name, network_id, mac, created_at, updated_at) "
                       "VALUES ('bad', ?, '02:a7:00:00:00:01', 'n', 'n')", (self.dual,))
        db.close()

    # ---------------------------------------------------------------- deleting
    def test_delete_releases_entry_mac_and_both_ips(self):
        e = self.mk("a", self.dual, "192.0.2.5", "2001:db8:abcd:4::5").get_json()
        self.assertEqual(self.client.delete(f"/api/entries/{e['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/entries/{e['id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/entries?mac={e['mac']}").get_json(), [])
        self.assertEqual(self.mk("again", self.dual, "192.0.2.5", "2001:db8:abcd:4::5").status_code, 201)

    # -------------------------------------------------------------------- macs
    def test_generated_macs_are_unique(self):
        macs = set()
        for i in range(2, 200):
            r = self.mk(f"c{i}", self.dual, ip4=f"192.0.2.{i}")
            self.assertEqual(r.status_code, 201)
            macs.add(r.get_json()["mac"])
        self.assertEqual(len(macs), 198)

    def test_mac_generation_retries_on_collision(self):
        import secrets
        real = secrets.randbelow
        seq = iter([1, 2, 3, 4, 9, 9, 9, 9])  # first try -> 02:a7:01:02:03:04, then 02:a7:09:09:09:09

        def fake(n):
            try:
                return next(seq)
            except StopIteration:
                return real(n)

        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO entries (name, network_id, ip4, mac, notes, created_at, updated_at) "
                   "VALUES ('x', ?, '192.0.2.99', '02:a7:01:02:03:04', '', 'now', 'now')", (self.dual,))
        db.commit()
        db.close()
        self.appmod.secrets.randbelow = fake
        try:
            r = self.mk("b", self.dual, ip4="192.0.2.6")
        finally:
            self.appmod.secrets.randbelow = real
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["mac"], "02:a7:09:09:09:09")

    def test_lookup_by_either_ip_and_data_persists(self):
        e = self.mk("a", self.dual, "192.0.2.5", "2001:db8:abcd:4::5").get_json()
        for ip in ("192.0.2.5", "2001:DB8:ABCD:4:0:0:0:5"):
            found = self.client.get(f"/api/entries?ip={ip}").get_json()
            self.assertEqual([x["mac"] for x in found], [e["mac"]])
        again = importlib.reload(self.appmod).app.test_client().get("/api/entries").get_json()
        self.assertEqual(again[0]["mac"], e["mac"])

    def test_bad_prefix_rejected(self):
        for bad in ("01:00", "00:11", "zz", "02:a7:00:00:00:00"):
            with self.assertRaises(ValueError):
                self.appmod.parse_prefix(bad)
        self.assertEqual(self.appmod.parse_prefix("0a"), [0x0A])

    def test_ui_and_health(self):
        self.assertEqual(self.client.get("/healthz").get_json(), {"status": "ok"})
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"macreg", r.data)
        r.close()


class MigrationTests(unittest.TestCase):
    """The first version stored one subnet per network and one IP per entry."""

    V1 = """
    CREATE TABLE subnets (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, cidr TEXT NOT NULL UNIQUE);
    CREATE TABLE entries (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        subnet_id INTEGER NOT NULL REFERENCES subnets(id) ON DELETE RESTRICT,
        ip TEXT NOT NULL, mac TEXT NOT NULL UNIQUE, notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (subnet_id, ip));
    CREATE TRIGGER entries_mac_immutable BEFORE UPDATE OF mac ON entries WHEN NEW.mac != OLD.mac
    BEGIN SELECT RAISE(ABORT, 'mac address is immutable'); END;
    INSERT INTO subnets VALUES (1, 'vlan20', '203.0.113.0/24'), (2, 'vlan20-v6', 'fd00:20::/64');
    INSERT INTO entries VALUES
        (1, 'jellyfin', 1, '203.0.113.55', '02:a7:aa:aa:aa:01', 'media', 't1', 't1'),
        (2, 'homepage', 2, 'fd00:20::10', '02:a7:aa:aa:aa:02', '', 't2', 't2');
    """

    def test_v1_database_is_converted_keeping_ids_and_macs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            db = sqlite3.connect(path)
            db.executescript(self.V1)
            db.close()

            appmod = load_app(path)
            client = appmod.app.test_client()

            nets = {n["name"]: n for n in client.get("/api/networks").get_json()}
            self.assertEqual((nets["vlan20"]["cidr4"], nets["vlan20"]["cidr6"]), ("203.0.113.0/24", None))
            self.assertEqual((nets["vlan20-v6"]["cidr4"], nets["vlan20-v6"]["cidr6"]), (None, "fd00:20::/64"))

            entries = {e["name"]: e for e in client.get("/api/entries").get_json()}
            self.assertEqual((entries["jellyfin"]["id"], entries["jellyfin"]["ip4"], entries["jellyfin"]["ip6"],
                              entries["jellyfin"]["mac"], entries["jellyfin"]["notes"]),
                             (1, "203.0.113.55", None, "02:a7:aa:aa:aa:01", "media"))
            self.assertEqual((entries["homepage"]["ip4"], entries["homepage"]["ip6"], entries["homepage"]["mac"]),
                             (None, "fd00:20::10", "02:a7:aa:aa:aa:02"))

            # backup written, old tables gone, immutability still enforced, new features usable
            self.assertTrue(os.path.exists(path + ".pre-dualstack.bak"))
            db = sqlite3.connect(path)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("subnets", tables)
            self.assertNotIn("entries_v1", tables)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE entries SET mac = '02:a7:00:00:00:00' WHERE id = 1")
            db.close()

            r = client.put(f"/api/networks/{nets['vlan20']['id']}", json={"cidr6": "fd00:20::/64"})
            self.assertEqual(r.status_code, 409)  # fd00:20::/64 already belongs to vlan20-v6 -> unique
            r = client.put(f"/api/entries/1", json={"ip4": "203.0.113.55"})
            self.assertEqual(r.status_code, 200)

            # running init again on the migrated file is a no-op
            appmod2 = load_app(path)
            self.assertEqual(len(appmod2.app.test_client().get("/api/entries").get_json()), 2)


if __name__ == "__main__":
    unittest.main()
