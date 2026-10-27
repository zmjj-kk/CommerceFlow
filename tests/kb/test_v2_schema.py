import pytest

from app.kb import milvus_client as mc


class Schema:
    def __init__(self):
        self.fields = {}
        self.functions = []

    def add_field(self, name, dtype, **kw):
        self.fields[name] = {"name": name, "type": dtype, **kw}

    def add_function(self, function):
        self.functions.append(function)


class Indexes:
    def __init__(self):
        self.indexes = []

    def add_index(self, **kw):
        self.indexes.append(kw)


class Client:
    def __init__(self):
        self.schema = None
        self.indexes = None

    def has_collection(self, name):
        return self.schema is not None

    def create_schema(self, **kw):
        return Schema()

    def prepare_index_params(self):
        return Indexes()

    def create_collection(self, name, schema, index_params):
        self.schema, self.indexes = schema, index_params

    def describe_collection(self, name):
        return {"fields": list(self.schema.fields.values())}

    def load_collection(self, name):
        pass


def test_v2_schema_reuses_analyzer_metrics_and_supports_reopen():
    client = Client()
    mc.ensure_collection(client, "knowledge_v2_test", v2=True)
    assert set(mc.V2_OUTPUT).issubset(client.schema.fields)
    assert client.schema.fields["dense"]["dim"] == mc.DIM
    assert client.schema.fields["text"]["analyzer_params"] == {"type": "chinese"}
    assert {i["metric_type"] for i in client.indexes.indexes} == {"COSINE", "BM25"}
    assert len(client.schema.functions) == 1
    mc.ensure_collection(client, "knowledge_v2_test", v2=True)


def test_v2_cannot_overwrite_or_reinterpret_legacy_schema():
    client = Client()
    with pytest.raises(ValueError):
        mc.ensure_collection(client, "knowledge", v2=True)
    mc.ensure_collection(client, "legacy_test")
    with pytest.raises(ValueError, match="not a v2"):
        mc.ensure_collection(client, "legacy_test", v2=True)


def test_reopening_v2_with_wrong_embedding_dimension_fails():
    client = Client()
    mc.ensure_collection(client, "knowledge_v2_test", v2=True)
    client.schema.fields["dense"]["dim"] = mc.DIM + 1
    with pytest.raises(ValueError, match="dimension mismatch"):
        mc.ensure_collection(client, "knowledge_v2_test", v2=True)
