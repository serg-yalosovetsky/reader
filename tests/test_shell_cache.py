import pytest


@pytest.mark.parametrize('asset', [
    '/vendor/foliate-js/vendor/pdfjs/pdf.mjs?v=20261016-1',
    '/vendor/foliate-js/vendor/pdfjs/pdf.worker.mjs?v=20261016-1',
    '/vendor/foliate-js/vendor/pdfjs/compat.mjs?v=20261016-1',
    '/manifest.webmanifest', '/favicon.svg',
])
def test_shell_revalidates(client, asset):
    response = client.get(asset)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-cache, must-revalidate'


def test_reader_url_module_is_available_offline(client):
    assets = client.get('/api/shell-assets').json()['assets']
    assert '/js/core/reader-url.js' in assets
