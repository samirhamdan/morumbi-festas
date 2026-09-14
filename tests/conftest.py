"""Fixtures compartilhadas."""

import os
import tempfile

import pytest

os.environ["FESTAS_SENHA"] = "teste123"

from sistema.app import criar_app
from sistema import dados


@pytest.fixture()
def app(tmp_path):
    db = str(tmp_path / "teste.db")
    dados.CAMINHO_BD = db
    aplicacao = criar_app()
    aplicacao.config["TESTING"] = True
    yield aplicacao


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def logado(client):
    client.post("/entrar", data={"login": "admin", "senha": "teste123"})
    return client


@pytest.fixture()
def admin(logado):
    return logado
