"""Testes da sincronizacao de clientes entre Morumbi 3D e Morumbi Festas."""
import sqlite3
import tempfile
import os

import pytest

from ferramentas import sincronizar_clientes as sync


ESQUEMA_3D = """
CREATE TABLE IF NOT EXISTS clientes (
    id         INTEGER PRIMARY KEY,
    nome       TEXT NOT NULL,
    whatsapp   TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    cpf        TEXT NOT NULL DEFAULT '',
    endereco   TEXT NOT NULL DEFAULT '',
    complemento TEXT NOT NULL DEFAULT '',
    bairro     TEXT NOT NULL DEFAULT '',
    cidade     TEXT NOT NULL DEFAULT '',
    cep        TEXT NOT NULL DEFAULT '',
    canal      TEXT NOT NULL DEFAULT '',
    observacao TEXT NOT NULL DEFAULT '',
    ativo      INTEGER NOT NULL DEFAULT 1,
    criado_em  TEXT NOT NULL DEFAULT '',
    criado_por TEXT NOT NULL DEFAULT ''
);
"""

ESQUEMA_FESTAS = """
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    cpf_cnpj TEXT,
    whatsapp TEXT,
    telefone TEXT,
    email TEXT,
    data_nascimento TEXT,
    endereco TEXT,
    bairro TEXT,
    cidade TEXT DEFAULT 'Campo Grande',
    cep TEXT,
    instagram TEXT,
    observacoes TEXT,
    origem TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ativo',
    cliente_3d_id INTEGER,
    criado_em TEXT NOT NULL DEFAULT '',
    atualizado_em TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tags_cliente (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id),
    tag TEXT NOT NULL,
    UNIQUE(cliente_id, tag)
);
"""


@pytest.fixture
def bancos(tmp_path):
    """Cria bancos temporarios para 3D e Festas."""
    db_festas = str(tmp_path / "festas.db")
    db_3d = str(tmp_path / "morumbi3d" / "sistema.sqlite3")
    os.makedirs(tmp_path / "morumbi3d")

    conn_f = sqlite3.connect(db_festas)
    conn_f.row_factory = sqlite3.Row
    conn_f.executescript(ESQUEMA_FESTAS)
    conn_f.commit()
    conn_f.close()

    conn_3 = sqlite3.connect(db_3d)
    conn_3.row_factory = sqlite3.Row
    conn_3.executescript(ESQUEMA_3D)
    conn_3.commit()
    conn_3.close()

    old_festas = sync.CAMINHO_FESTAS
    old_3d = sync.CAMINHO_3D
    sync.CAMINHO_FESTAS = db_festas
    sync.CAMINHO_3D = db_3d

    yield db_festas, db_3d

    sync.CAMINHO_FESTAS = old_festas
    sync.CAMINHO_3D = old_3d


def _inserir_3d(db_3d, nome, cpf="", whatsapp="", email="", canal="", ativo=1):
    conn = sqlite3.connect(db_3d)
    conn.execute(
        "INSERT INTO clientes (nome, cpf, whatsapp, email, canal, ativo, criado_em)"
        " VALUES (?,?,?,?,?,?,datetime('now'))",
        (nome, cpf, whatsapp, email, canal, ativo))
    conn.commit()
    last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return last_id


def _inserir_festas(db_festas, nome, cpf_cnpj="", whatsapp="", email="",
                    origem="", cliente_3d_id=None):
    conn = sqlite3.connect(db_festas)
    conn.execute(
        "INSERT INTO clientes (nome, cpf_cnpj, whatsapp, email, origem,"
        " cliente_3d_id, criado_em, atualizado_em)"
        " VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (nome, cpf_cnpj, whatsapp, email, origem, cliente_3d_id))
    conn.commit()
    last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return last_id


def _contar(db, tabela="clientes"):
    conn = sqlite3.connect(db)
    n = conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
    conn.close()
    return n


def _buscar(db, id_):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM clientes WHERE id = ?", (id_,)).fetchone()
    conn.close()
    return dict(r) if r else None


# --- 3D -> Festas ---

def test_cliente_3d_criado_no_festas(bancos):
    db_festas, db_3d = bancos
    _inserir_3d(db_3d, "Maria Silva", cpf="12345678901", whatsapp="5567999990001")

    resultado = sync.sincronizar()
    assert resultado["3d_para_festas"]["criados"] == 1
    assert _contar(db_festas) == 1

    conn = sqlite3.connect(db_festas)
    conn.row_factory = sqlite3.Row
    c = dict(conn.execute("SELECT * FROM clientes LIMIT 1").fetchone())
    conn.close()
    assert c["nome"] == "Maria Silva"
    assert c["cpf_cnpj"] == "12345678901"
    assert c["cliente_3d_id"] == 1


def test_cliente_3d_ja_existe_nao_duplica(bancos):
    db_festas, db_3d = bancos
    id_3d = _inserir_3d(db_3d, "Maria Silva", cpf="12345678901")
    _inserir_festas(db_festas, "Maria Silva", cpf_cnpj="12345678901", cliente_3d_id=id_3d)

    resultado = sync.sincronizar()
    assert resultado["3d_para_festas"]["criados"] == 0
    assert _contar(db_festas) == 1


def test_cliente_3d_match_por_cpf_vincula(bancos):
    db_festas, db_3d = bancos
    id_3d = _inserir_3d(db_3d, "Maria Silva", cpf="12345678901")
    id_festas = _inserir_festas(db_festas, "Maria S.", cpf_cnpj="12345678901")

    resultado = sync.sincronizar()
    assert resultado["3d_para_festas"]["vinculados"] == 1
    assert resultado["3d_para_festas"]["criados"] == 0

    c = _buscar(db_festas, id_festas)
    assert c["cliente_3d_id"] == id_3d


def test_cliente_3d_match_por_whatsapp_vincula(bancos):
    db_festas, db_3d = bancos
    id_3d = _inserir_3d(db_3d, "Joao", whatsapp="5567999990002")
    id_festas = _inserir_festas(db_festas, "Joao Santos", whatsapp="5567999990002")

    resultado = sync.sincronizar()
    assert resultado["3d_para_festas"]["vinculados"] == 1
    c = _buscar(db_festas, id_festas)
    assert c["cliente_3d_id"] == id_3d


def test_cliente_3d_match_por_email_vincula(bancos):
    db_festas, db_3d = bancos
    id_3d = _inserir_3d(db_3d, "Ana", email="ana@teste.com")
    id_festas = _inserir_festas(db_festas, "Ana Paula", email="ana@teste.com")

    resultado = sync.sincronizar()
    assert resultado["3d_para_festas"]["vinculados"] == 1
    c = _buscar(db_festas, id_festas)
    assert c["cliente_3d_id"] == id_3d


def test_cliente_3d_inativo_nao_sincroniza(bancos):
    db_festas, db_3d = bancos
    _inserir_3d(db_3d, "Inativo", cpf="99999999999", ativo=0)

    resultado = sync.sincronizar()
    assert resultado["3d_para_festas"]["criados"] == 0
    assert _contar(db_festas) == 0


# --- Festas -> 3D ---

def test_cliente_festas_criado_no_3d(bancos):
    db_festas, db_3d = bancos
    _inserir_festas(db_festas, "Pedro Lima", cpf_cnpj="98765432100", whatsapp="5567888880001")

    resultado = sync.sincronizar()
    assert resultado["festas_para_3d"]["criados"] == 1
    assert _contar(db_3d) == 1

    conn = sqlite3.connect(db_3d)
    conn.row_factory = sqlite3.Row
    c = dict(conn.execute("SELECT * FROM clientes LIMIT 1").fetchone())
    conn.close()
    assert c["nome"] == "Pedro Lima"
    assert c["cpf"] == "98765432100"


def test_cliente_festas_vincula_ao_3d(bancos):
    db_festas, db_3d = bancos
    id_3d = _inserir_3d(db_3d, "Carlos", cpf="11111111111")
    id_festas = _inserir_festas(db_festas, "Carlos Souza", cpf_cnpj="11111111111")

    resultado = sync.sincronizar()
    total_vinculados = (resultado["3d_para_festas"]["vinculados"]
                        + resultado["festas_para_3d"]["vinculados"])
    assert total_vinculados == 1
    assert resultado["3d_para_festas"]["criados"] == 0
    assert resultado["festas_para_3d"]["criados"] == 0

    c = _buscar(db_festas, id_festas)
    assert c["cliente_3d_id"] == id_3d
    assert _contar(db_3d) == 1


def test_cliente_festas_inativo_nao_sincroniza(bancos):
    db_festas, db_3d = bancos
    conn = sqlite3.connect(db_festas)
    conn.execute(
        "INSERT INTO clientes (nome, cpf_cnpj, status, criado_em, atualizado_em)"
        " VALUES ('Inativo', '22222222222', 'inativo', datetime('now'), datetime('now'))")
    conn.commit()
    conn.close()

    resultado = sync.sincronizar()
    assert resultado["festas_para_3d"]["criados"] == 0
    assert _contar(db_3d) == 0


# --- Bidirecional ---

def test_sync_bidirecional(bancos):
    db_festas, db_3d = bancos
    _inserir_3d(db_3d, "Do 3D", cpf="33333333333")
    _inserir_festas(db_festas, "Do Festas", cpf_cnpj="44444444444")

    resultado = sync.sincronizar()
    assert resultado["3d_para_festas"]["criados"] == 1
    assert resultado["festas_para_3d"]["criados"] == 1
    assert _contar(db_festas) == 2
    assert _contar(db_3d) == 2


def test_sync_idempotente(bancos):
    db_festas, db_3d = bancos
    _inserir_3d(db_3d, "Maria", cpf="55555555555")
    _inserir_festas(db_festas, "Pedro", cpf_cnpj="66666666666")

    sync.sincronizar()
    r2 = sync.sincronizar()

    assert r2["3d_para_festas"]["criados"] == 0
    assert r2["3d_para_festas"]["vinculados"] == 0
    assert r2["festas_para_3d"]["criados"] == 0
    assert r2["festas_para_3d"]["vinculados"] == 0
    assert _contar(db_festas) == 2
    assert _contar(db_3d) == 2


def test_campos_mapeados_corretamente_3d_para_festas(bancos):
    db_festas, db_3d = bancos
    _inserir_3d(db_3d, "Teste Campos", cpf="77777777777", whatsapp="5567999991234",
                email="teste@mail.com", canal="Instagram")

    sync.sincronizar()

    conn = sqlite3.connect(db_festas)
    conn.row_factory = sqlite3.Row
    c = dict(conn.execute("SELECT * FROM clientes LIMIT 1").fetchone())
    conn.close()

    assert c["nome"] == "Teste Campos"
    assert c["cpf_cnpj"] == "77777777777"
    assert c["whatsapp"] == "5567999991234"
    assert c["email"] == "teste@mail.com"
    assert c["origem"] == "Instagram"
    assert c["status"] == "ativo"
    assert c["cliente_3d_id"] is not None


def test_campos_mapeados_corretamente_festas_para_3d(bancos):
    db_festas, db_3d = bancos
    _inserir_festas(db_festas, "Teste Reverso", cpf_cnpj="88888888888",
                    whatsapp="5567888881234", email="rev@mail.com", origem="WhatsApp")

    sync.sincronizar()

    conn = sqlite3.connect(db_3d)
    conn.row_factory = sqlite3.Row
    c = dict(conn.execute("SELECT * FROM clientes LIMIT 1").fetchone())
    conn.close()

    assert c["nome"] == "Teste Reverso"
    assert c["cpf"] == "88888888888"
    assert c["whatsapp"] == "5567888881234"
    assert c["email"] == "rev@mail.com"
    assert c["canal"] == "WhatsApp"
    assert c["criado_por"] == "sync_festas"
