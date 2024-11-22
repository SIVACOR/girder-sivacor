import pytest

from girder.plugin import loadedPlugins


@pytest.mark.plugin('girder_sivacor')
def test_import(server):
    assert 'girder_sivacor' in loadedPlugins()
