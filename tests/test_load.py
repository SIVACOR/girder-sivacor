import pytest

from girder.plugin import loadedPlugins


@pytest.mark.plugin('sivacor')
def test_import(server):
    assert 'sivacor' in loadedPlugins()
