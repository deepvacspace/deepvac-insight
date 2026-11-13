"""Tests of the chamber registry."""

import pytest

from app.services import chambers_service

pytestmark = pytest.mark.integration


def test_first_connect_seeds_a_default_chamber(deepvac_data_dir):
    chambers = chambers_service.list_chambers()
    assert len(chambers) == 1
    assert chambers[0]["name"] == "Chamber 1"
    assert chambers[0]["host"] == "127.0.0.1"
    assert chambers[0]["port"] == 5555


def test_add_chamber(deepvac_data_dir):
    chamber = chambers_service.add_chamber("Chamber 2", "10.0.0.5", 6000)
    assert chamber["name"] == "Chamber 2"
    assert chamber["host"] == "10.0.0.5"
    assert chamber["port"] == 6000
    names = {c["name"] for c in chambers_service.list_chambers()}
    assert names == {"Chamber 1", "Chamber 2"}


def test_add_chamber_duplicate_name_raises(deepvac_data_dir):
    with pytest.raises(chambers_service.ChamberError):
        chambers_service.add_chamber("Chamber 1", "10.0.0.9", 7000)


def test_add_chamber_empty_name_raises(deepvac_data_dir):
    with pytest.raises(chambers_service.ChamberError):
        chambers_service.add_chamber("   ", "127.0.0.1", 5555)


def test_add_chamber_invalid_port_raises(deepvac_data_dir):
    with pytest.raises(chambers_service.ChamberError):
        chambers_service.add_chamber("Chamber X", "127.0.0.1", 70000)


def test_update_chamber(deepvac_data_dir):
    chamber = chambers_service.add_chamber("Chamber 2", "10.0.0.5", 6000)
    updated = chambers_service.update_chamber(chamber["id"], "Chamber 2 Renamed", "10.0.0.6", 6001)
    assert updated["name"] == "Chamber 2 Renamed"
    assert updated["host"] == "10.0.0.6"
    assert updated["port"] == 6001


def test_update_chamber_to_duplicate_name_raises(deepvac_data_dir):
    chamber = chambers_service.add_chamber("Chamber 2", "10.0.0.5", 6000)
    with pytest.raises(chambers_service.ChamberError):
        chambers_service.update_chamber(chamber["id"], "Chamber 1", "10.0.0.5", 6000)


def test_delete_chamber(deepvac_data_dir):
    chamber = chambers_service.add_chamber("Chamber 2", "10.0.0.5", 6000)
    chambers_service.delete_chamber(chamber["id"])
    names = {c["name"] for c in chambers_service.list_chambers()}
    assert names == {"Chamber 1"}


def test_chambers_survive_a_fresh_connection(deepvac_data_dir):
    chambers_service.add_chamber("Chamber 2", "10.0.0.5", 6000)
    chambers_again = chambers_service.list_chambers()
    assert len(chambers_again) == 2
