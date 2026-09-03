"""
test_alerts.py — grouping, severity ordering, the per-cycle cap and
shadow-mode annotation.

alerts.py's own docstring calls alert volume "the real design
constraint", and a flood is functionally identical to no alerts. That
makes the cap and the grouping worth testing properly: they are the
difference between a monitor you read and one you mute.
"""
import pytest

import alerts


def ev(strike, side="c", rule="ema", raw=0.2, adj=0.18, severity="warn",
       would_suppress=False, spot_move=0.001, atm_change=0.02):
    return {"strike": strike, "side": side, "rule": rule,
            "raw_pct_change": raw, "adj_pct_change": adj,
            "severity": severity, "would_suppress": would_suppress,
            "spot_move_pct": spot_move, "atm_iv_change_pct": atm_change}


@pytest.fixture
def sent(monkeypatch):
    """Captures messages instead of calling Telegram."""
    out = []
    monkeypatch.setattr(alerts, "send", lambda m: out.append(m) or True)
    return out


class TestGrouping:
    def test_adjacent_strikes_collapse(self):
        groups = alerts.group_events([ev(3400), ev(3425), ev(3450)])
        assert len(groups) == 1
        assert groups[0]["count"] == 3
        assert groups[0]["strike_range"] == "3400-3450"

    def test_distant_strikes_split(self):
        groups = alerts.group_events([ev(3400), ev(3800)])
        assert len(groups) == 2

    def test_boundary_distance_is_inclusive(self):
        """STRIKE_GROUP_DISTANCE is 30 and the test is `<=`."""
        assert len(alerts.group_events([ev(3400), ev(3430)])) == 1
        assert len(alerts.group_events([ev(3400), ev(3431)])) == 2

    def test_sides_never_merge(self):
        assert len(alerts.group_events([ev(3400, side="c"),
                                        ev(3400, side="p")])) == 2

    def test_rules_never_merge(self):
        assert len(alerts.group_events([ev(3400, rule="ema"),
                                        ev(3400, rule="drift_3h")])) == 2

    def test_unsorted_input_is_grouped_correctly(self):
        groups = alerts.group_events([ev(3450), ev(3400), ev(3425)])
        assert len(groups) == 1

    def test_single_strike_range_has_no_dash(self):
        assert alerts.group_events([ev(3400)])[0]["strike_range"] == "3400"

    def test_group_takes_the_highest_severity_present(self):
        g = alerts.group_events([ev(3400, severity="info"),
                                 ev(3425, severity="high")])[0]
        assert g["severity"] == "high"

    def test_group_takes_the_largest_raw_change(self):
        g = alerts.group_events([ev(3400, raw=0.2), ev(3425, raw=0.5)])[0]
        assert g["max_raw"] == 0.5


class TestShadowMode:
    def test_a_group_is_only_filtered_if_every_member_would_be(self):
        """One survivor means the move was real somewhere in the range."""
        g = alerts.group_events([ev(3400, would_suppress=True),
                                 ev(3425, would_suppress=False)])[0]
        assert g["all_suppressed"] is False

    def test_fully_suppressed_group_is_marked(self):
        g = alerts.group_events([ev(3400, would_suppress=True),
                                 ev(3425, would_suppress=True)])[0]
        assert g["all_suppressed"] is True
        assert "smile roll" in alerts.format_group("GOLD", g)

    def test_raw_mode_still_sends_suppressed_groups(self, sent, monkeypatch):
        monkeypatch.setattr(alerts.config, "ALERT_ON_RAW", True)
        alerts.notify("GOLD", [ev(3400, would_suppress=True)])
        assert len(sent) == 1

    def test_live_mode_drops_them(self, sent, monkeypatch):
        monkeypatch.setattr(alerts.config, "ALERT_ON_RAW", False)
        alerts.notify("GOLD", [ev(3400, would_suppress=True)])
        assert sent == []

    def test_live_mode_keeps_genuine_events(self, sent, monkeypatch):
        monkeypatch.setattr(alerts.config, "ALERT_ON_RAW", False)
        alerts.notify("GOLD", [ev(3400, would_suppress=False)])
        assert len(sent) == 1


class TestFormatting:
    def test_headline_contains_the_essentials(self):
        g = alerts.group_events([ev(3400, side="p", rule="drift_3h")])[0]
        msg = alerts.format_group("GOLD", g)
        assert "PUT" in msg and "drift_3h" in msg and "3400" in msg

    def test_count_shown_only_for_multi_strike_groups(self):
        one = alerts.format_group("GOLD", alerts.group_events([ev(3400)])[0])
        many = alerts.format_group(
            "GOLD", alerts.group_events([ev(3400), ev(3425)])[0])
        assert "(x" not in one and "(x2)" in many

    def test_unjudgeable_adjustment_says_so(self):
        g = alerts.group_events([ev(3400, adj=None)])[0]
        assert "n/a" in alerts.format_group("GOLD", g)

    def test_negative_adjustment_renders_with_a_sign(self):
        g = alerts.group_events([ev(3400, adj=-0.05)])[0]
        assert "-5.0%" in alerts.format_group("GOLD", g)

    def test_optional_context_lines_are_omitted_when_unknown(self):
        g = alerts.group_events([ev(3400, spot_move=None, atm_change=None)])[0]
        msg = alerts.format_group("GOLD", g)
        assert "Spot moved" not in msg and "ATM IV" not in msg

    def test_all_severities_have_an_icon(self):
        for sev in ("info", "warn", "high"):
            g = alerts.group_events([ev(3400, severity=sev)])[0]
            assert alerts.format_group("GOLD", g)


class TestCapAndOrdering:
    def test_cap_is_enforced_with_an_overflow_notice(self, sent, monkeypatch):
        monkeypatch.setattr(alerts.config, "MAX_ALERTS_PER_CYCLE", 2)
        monkeypatch.setattr(alerts.config, "ALERT_ON_RAW", True)
        events = [ev(3000 + 200 * i) for i in range(6)]
        alerts.notify("GOLD", events)
        assert len(sent) == 3           # 2 groups + 1 overflow line
        assert "more GOLD groups" in sent[-1]

    def test_no_overflow_notice_when_under_the_cap(self, sent, monkeypatch):
        monkeypatch.setattr(alerts.config, "MAX_ALERTS_PER_CYCLE", 10)
        alerts.notify("GOLD", [ev(3400)])
        assert len(sent) == 1

    def test_high_severity_is_sent_first(self, sent, monkeypatch):
        monkeypatch.setattr(alerts.config, "MAX_ALERTS_PER_CYCLE", 1)
        alerts.notify("GOLD", [ev(3000, severity="info"),
                               ev(4000, severity="high")])
        assert "4000" in sent[0]

    def test_within_a_severity_the_larger_move_wins(self, sent, monkeypatch):
        monkeypatch.setattr(alerts.config, "MAX_ALERTS_PER_CYCLE", 1)
        alerts.notify("GOLD", [ev(3000, adj=0.1), ev(4000, adj=0.9)])
        assert "4000" in sent[0]

    def test_empty_event_list_sends_nothing(self, sent):
        alerts.notify("GOLD", [])
        assert sent == []

    @pytest.mark.xfail(reason="sort key uses `max_adj or max_raw`; 0.0 is "
                              "falsy so a fully smile-explained group is "
                              "ranked by its large raw change",
                       strict=False)
    def test_zero_adjustment_should_not_outrank_a_real_move(
            self, sent, monkeypatch):
        """LATENT BUG — asserts the DESIRED behaviour, so this flips to
        XPASS once fixed.

        The sort key is `-(g["max_adj"] or g["max_raw"])`. An adjusted
        change of exactly 0.0 is falsy, so the group is ranked by its
        RAW change instead — which for a perfectly smile-explained move
        is large. The one group we are most confident is noise sorts as
        if it were the most significant, and with a tight cap it can
        push a real event out of the cycle entirely.

        Fix: `-(g["max_adj"] if g["max_adj"] is not None else
        g["max_raw"])`.
        """
        monkeypatch.setattr(alerts.config, "MAX_ALERTS_PER_CYCLE", 1)
        alerts.notify("GOLD", [ev(3000, raw=0.9, adj=0.0),
                               ev(4000, raw=0.2, adj=0.15)])
        assert "4000" in sent[0], "the genuine move should have been sent"


class TestSendGuards:
    def test_unconfigured_telegram_prints_instead_of_raising(
            self, monkeypatch, capsys):
        monkeypatch.setattr(alerts.config, "TELEGRAM_BOT_TOKEN", None)
        monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", None)
        assert alerts.send("hello") is False
        assert "would have sent" in capsys.readouterr().out

    def test_a_telegram_outage_does_not_raise(self, monkeypatch, fake_db):
        monkeypatch.setattr(alerts.config, "TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", "c")
        monkeypatch.setattr(alerts.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(
                                OSError("network down")))
        assert alerts.send("hello") is False

    def test_alerted_column_is_never_set(self):
        """GAP, not a crash. spike_events has an `alerted` boolean, but
        nothing in alerts.py or detector.py ever writes it. Any future
        query of the form 'which detections actually reached me' will
        return nothing. Either populate it after a successful send or
        drop the column."""
        import inspect
        assert "alerted" not in inspect.getsource(alerts)
