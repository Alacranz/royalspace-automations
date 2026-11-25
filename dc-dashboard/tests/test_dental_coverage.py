from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock, patch

from app.callgrid.coverage import CAMPAIGN, endpoint_rows, observe, view_filters

NOW = datetime(2026, 11, 25, 16, 22, tzinfo=timezone.utc)


def ping(cid, **changes):
    return {
        "CallId": cid, "CampaignId": CAMPAIGN, "createdAt": NOW.isoformat(),
        "BuyerName": "(20) Buyer A", "DestinationId": "dest-a", "ZipCode": "01234",
        "IVRZip": "01234", "StateCodeFromZipCode": "MA", "RoutingPlanName": "Spanish - Messenger",
        "PingAccepted": 1, "PingBidAmount": 20, "PingWonCall": 1, **changes,
    }


def call(cid, **changes):
    return {
        "id": cid, "CampaignId": CAMPAIGN, "createdAt": NOW.isoformat(), "callStatus": "COMPLETED",
        "DestinationId": "dest-a", "CallDurationFromConnect": 90, "CallRevenue": "20.00", "CallBillable": True, **changes,
    }


class CoverageTests(TestCase):
    def test_counts_one_attempt_per_buyer_and_call(self):
        pings = [ping("a"), ping("a"), ping("b"), ping("c", PingAccepted=0, PingResponseCode="1006")]
        calls = [call("a"), call("b", CallBillable=False)]
        result = observe(pings, calls, NOW)
        self.assertEqual(1, len(result))
        cell = result[0]
        self.assertEqual("01234", cell["zip"])
        self.assertEqual((3, 2, 2, 1), tuple(cell[k] for k in ("attempts", "offers", "connected", "billable")))
        self.assertEqual("Señal repetida", cell["status"])
        self.assertNotIn("a", cell.values())

    def test_accepted_is_not_connected_if_other_destination_wins_or_in_progress(self):
        c = [call("a", DestinationId="other"), call("b", callStatus="IN_PROGRESS")]
        row = observe([ping("a"), ping("b")], c, NOW)[0]
        self.assertEqual((2, 0, 1), (row["offers"], row["connected"], row["in_progress"]))

    def test_ignores_ambiguous_zip_estimate_and_unpriced_static_accept(self):
        p = [ping("a"), ping("a", ZipCode="01235", IVRZip="01235"),
             ping("b", IVRZip="", InboundZipCode="01234"),
             ping("c", PingBidAmount=0)]
        rows = observe(p, [call("a"), call("b"), call("c")], NOW)
        self.assertEqual(1, len(rows))
        self.assertEqual((1, 0, 1), (rows[0]["attempts"], rows[0]["offers"], rows[0]["connected"]))

    def test_landing_provenance_and_language_are_independent(self):
        r = observe([ping("a"), ping("b", RoutingPlanName="English - LP", VisitorZipCode="01234", IVRZip="")],
                    [], NOW)
        self.assertEqual({"ES", "EN"}, {a["language"] for a in r})

    def test_old_connects_do_not_label_current_coverage(self):
        old = (NOW-timedelta(minutes=42)).isoformat()
        a = [ping("a", createdAt=old), ping("b", createdAt=old), ping("c")]
        rows = observe(a, [call("a", createdAt=old), call("b", createdAt=old)], NOW)
        self.assertEqual("Conexión aislada", rows[0]["status"])

    def test_rejected_sample_vs_technical_rejection(self):
        a = [ping(str(i), PingAccepted=0, PingResponseCode="1006") for i in range(3)]
        a += [ping("d", PingAccepted=0, BuyerName="Buyer B", PingTimeout=0, PingApiFailed=1)]
        rows = {r["buyer"]:r for r in observe(a, [],NOW)}
        self.assertEqual("Sin puja observada", rows["Buyer A"]["status"])
        self.assertEqual("Error o límite técnico", rows["Buyer B"]["status"])

    def test_static_call_only_with_official_billable_flag(self):
        static = call("static", BuyerName="(1521)ClickDealer", DestinationId="static",
                      RoutingPlanName="Spanish - Messenger", ZipCode="92211", IVRZip="92211",
                      CallDurationFromConnect=2, CallRevenue="9", CallBillable="false")
        row = observe([], [static, static], NOW)[0]
        self.assertEqual(("ClickDealer", 0, 1, 0), tuple(row[k] for k in ("buyer", "offers", "connected", "billable")))
        self.assertEqual("Conexión aislada", row["status"])
        bids = [ping("static", BuyerName="ClickDealer", DestinationId="static", ZipCode="92211", IVRZip="92211")]
        row = observe(bids, [static], NOW)[0]
        self.assertEqual((1, 1, 1), tuple(row[k] for k in ("attempts", "offers", "connected")))

    def test_losing_offer_and_static_connection_attributed_separately(self):
        static = call("same", BuyerName="Buyer B", DestinationId="static",
                      RoutingPlanName="Spanish - Messenger", ZipCode="01234", IVRZip="01234",
                      CallBillable="true", CallRevenue="0")
        rows = {r["buyer"]: r for r in observe([ping("same")], [static], NOW)}
        self.assertEqual((1, 0), (rows["Buyer A"]["offers"], rows["Buyer A"]["connected"]))
        self.assertEqual((0, 1, 1), tuple(rows["Buyer B"][k] for k in ("offers", "connected", "billable")))
        static["IVRZip"] = "00001"
        self.assertEqual(["Buyer A"], [r["buyer"] for r in observe([ping("same")], [static], NOW)])

    @patch("app.callgrid.coverage.requests.get")
    def test_api_cursor_and_campaign_filter(self, get):
        replies = [{"data":[ping("a"), ping("foreign", CampaignId="other")],
                    "hasMore":True,"nextCursor":[123,"abc"]},
                   {"data":[ping("b")],"hasMore":False}]
        params_seen = []
        def send(url, **kw):
            params_seen.append(kw["params"].copy())
            self.assertTrue(url.endswith("/ping"))
            m = Mock()
            m.json.return_value = replies.pop(0)
            return m
        get.side_effect = send
        rows, complete = endpoint_rows("fake", "org", "ping", NOW-timedelta(minutes=30), NOW)
        self.assertTrue(complete)
        self.assertEqual(["a","b"], [r["CallId"] for r in rows])
        self.assertEqual('[123,"abc"]', params_seen[1]["searchAfter"])
        self.assertNotIn("apiKey",params_seen[0])
        self.assertIn(CAMPAIGN, params_seen[0]["filters"])

    @patch("app.callgrid.coverage.requests.get")
    def test_incomplete_without_cursor_is_not_empty_or_complete(self, get):
        get.return_value.json.return_value={"data":[ping("x")],"hasMore":True}
        result, complete=endpoint_rows("fake","org","ping",NOW-timedelta(minutes=30),NOW)
        self.assertFalse(complete)
        self.assertEqual(1,len(result))

    def test_buyer_stays_selected_when_it_ages_out(self):
        rows = [{"zip":"77356", "language":"ES", "buyer":"Rex Direct"}]
        v = view_filters(rows, "77356", "ES", "1800Dentist")
        self.assertEqual("1800Dentist", v["buyer"])
        self.assertIn("1800Dentist", v["buyers"])
        self.assertEqual([], v["rows"])

    def test_invalid_zip_does_not_clip_or_broaden(self):
        rows = [{"zip":"77356", "language":"ES", "buyer":"Rex Direct"}]
        for z in ("773560", "77356-1234", "7735X"):
            v = view_filters(rows, z, "ES")
            self.assertEqual(z, v["zip_code"])
            self.assertTrue(v["filter_error"])
            self.assertEqual([], v["rows"])

    @patch("app.callgrid.coverage.requests.get")
    def test_nested_call_envelope_and_bad_shape(self, get):
        get.return_value.json.return_value={"data":{"items":[call("a"),call("b")]},"hasMore":False}
        rows, complete = endpoint_rows("fake","org","call",NOW-timedelta(minutes=30),NOW)
        self.assertTrue(complete)
        self.assertEqual(["a","b"],[r["id"] for r in rows])
        get.return_value.json.return_value={"data":{"unexpected":[]}}
        with self.assertRaises(ValueError):
            endpoint_rows("fake","org","call",NOW-timedelta(minutes=30),NOW)
