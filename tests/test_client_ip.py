"""Far-side IP: Cloudflare header, then first XFF hop, then fallback."""

from debate.client_ip import client_ip


def test_cf_connecting_ip_wins_over_xff():
    assert (
        client_ip(
            {
                "cf-connecting-ip": "1.2.3.4",
                "x-forwarded-for": "9.9.9.9, 8.8.8.8",
            }
        )
        == "1.2.3.4"
    )


def test_cf_connecting_ip_title_case():
    assert client_ip({"CF-Connecting-IP": "5.6.7.8"}) == "5.6.7.8"


def test_cf_connecting_ip_is_stripped():
    assert client_ip({"cf-connecting-ip": "  1.1.1.1  "}) == "1.1.1.1"


def test_blank_cf_falls_through_to_xff():
    assert client_ip({"cf-connecting-ip": "  ", "x-forwarded-for": "4.4.4.4"}) == "4.4.4.4"


def test_first_hop_of_x_forwarded_for():
    assert client_ip({"x-forwarded-for": "10.0.0.1, 10.0.0.2, 10.0.0.3"}) == "10.0.0.1"


def test_x_forwarded_for_title_case():
    assert client_ip({"X-Forwarded-For": "8.8.8.8, 1.1.1.1"}) == "8.8.8.8"


def test_xff_first_hop_is_stripped():
    assert client_ip({"x-forwarded-for": "  7.7.7.7  , 2.2.2.2"}) == "7.7.7.7"


def test_fallback_when_headers_missing():
    assert client_ip({}, fallback="127.0.0.1") == "127.0.0.1"


def test_fallback_default_is_empty():
    assert client_ip({}) == ""


def test_blank_headers_use_fallback():
    assert client_ip({"cf-connecting-ip": "", "x-forwarded-for": "  "}, fallback="0.0.0.0") == "0.0.0.0"
