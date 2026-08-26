"""Human seat rules: token is the seat; name is a label; IP is never identity."""

import pytest

from debate.errors import FloorError
from debate.identity import IdentityBook


def test_token_reclaims_even_if_name_and_ip_change():
    book = IdentityBook()
    first = book.join("Vale", "1.1.1.1")
    first.connected = False
    second = book.join("Stranger", "8.8.8.8", token=first.token, watcher=True)
    assert second.session_id == first.session_id
    assert second.name == "Stranger"
    assert second.ip == "8.8.8.8"
    assert second.watcher is True
    assert second.connected is True
    assert book.humans() == [second]


def test_empty_name_is_rejected():
    book = IdentityBook()
    with pytest.raises(FloorError) as err:
        book.join("   ", "1.1.1.1")
    assert err.value.code == "invalid"


def test_fresh_seat_when_neither_matches():
    book = IdentityBook()
    a = book.join("Vale", "1.1.1.1")
    b = book.join("Bram", "2.2.2.2")
    assert a.session_id != b.session_id
    assert a.slot == 1
    assert b.slot == 2
    assert a.host is True
    assert b.host is False


def test_same_name_without_token_is_new_seat():
    book = IdentityBook()
    first = book.join("Vale", "1.1.1.1")
    second = book.join("Vale", "2.2.2.2")
    assert first.session_id != second.session_id
    assert first.token != second.token
    assert first.name == "Vale"
    assert second.name == "Vale"
    assert first.slot == 1
    assert second.slot == 2
    assert book.humans() == [first, second]


def test_same_ip_without_token_is_new_seat():
    book = IdentityBook()
    first = book.join("Vale", "1.1.1.1")
    second = book.join("Bram", "1.1.1.1")
    assert first.session_id != second.session_id
    assert first.token != second.token
    assert first.name == "Vale"
    assert second.name == "Bram"
    assert first.ip == "1.1.1.1"
    assert second.ip == "1.1.1.1"
    assert book.humans() == [first, second]


def test_unknown_token_mints_new_seat():
    book = IdentityBook()
    first = book.join("Vale", "1.1.1.1")
    second = book.join("Bram", "1.1.1.1", token="not-a-real-token")
    assert second.session_id != first.session_id
    assert second.token != first.token
    assert second.token != "not-a-real-token"
    assert second.name == "Bram"
    assert second.slot == 2
    assert book.humans() == [first, second]
