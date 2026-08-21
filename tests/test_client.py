import pytest

from bifrost.client import configured_services


def config(services):
    return {
        'signal': {'url': 'wss://example.test/signal', 'verify_tls': True},
        'local_http': {'host': '127.0.0.1', 'scheme': 'http'},
        'auth': {'private_key': 'private', 'public_key': 'public'},
        'services': services,
    }


def test_configured_services_supports_multiple_rooms():
    services = configured_services(config([
        {'room': 'home', 'local_port': 10080},
        {'room': 'office', 'local_port': 10081},
    ]))
    assert [(room, target) for room, target, _ in services] == [
        ('home', 'http://127.0.0.1:10080'),
        ('office', 'http://127.0.0.1:10081'),
    ]
    assert services[0][2]['signal']['room'] == 'home'
    assert services[1][2]['signal']['room'] == 'office'


def test_configured_services_rejects_duplicate_rooms():
    with pytest.raises(ValueError, match='duplicate room'):
        configured_services(config([
            {'room': 'home', 'local_port': 10080},
            {'room': 'home', 'local_port': 10081},
        ]))


def test_configured_services_rejects_invalid_port():
    with pytest.raises(ValueError, match='invalid local_port'):
        configured_services(config([{'room': 'home', 'local_port': 70000}]))
