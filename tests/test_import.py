import json_projection


def test_version():
    assert json_projection.__version__ == "0.2.0"


def test_stream_export():
    assert isinstance(json_projection.Projection(set()).stream(), json_projection.ProjectionStream)
