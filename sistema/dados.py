"""Camada de dados — SQLite."""

import json
import os
import sqlite3
from datetime import date, datetime

from sistema import formato

PERFIS = ("admin", "comercial", "operacional", "gestor")

CAMINHO_BD = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")


def conectar():
    conn = sqlite3.connect(CAMINHO_BD)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def inicializar():
    with conectar() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                login TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                perfil TEXT NOT NULL DEFAULT 'comercial',
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL DEFAULT (datetime('now')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS parametros (
                chave TEXT PRIMARY KEY,
                valor TEXT
            );

            CREATE TABLE IF NOT EXISTS organizacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                cnpj TEXT,
                telefone TEXT,
                whatsapp TEXT,
                email TEXT,
                endereco TEXT,
                bairro TEXT,
                cidade TEXT DEFAULT 'Campo Grande',
                cep TEXT,
                instagram TEXT,
                criado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER,
                tipo TEXT NOT NULL,
                descricao TEXT,
                dados TEXT,
                criado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)


# ---------------------------------------------------------------------------
# Auditoria
# ---------------------------------------------------------------------------

def registrar_acao(usuario_id, tipo: str, descricao: str, dados=None):
    d = json.dumps(dados, ensure_ascii=False) if dados else None
    with conectar() as conn:
        conn.execute(
            "INSERT INTO audit_log (usuario_id, tipo, descricao, dados, criado_em)"
            " VALUES (?, ?, ?, ?, ?)",
            (usuario_id, tipo, descricao, d, formato.agora()))


def listar_audit(limite=100) -> list:
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, u.nome AS usuario_nome FROM audit_log a"
            " LEFT JOIN usuarios u ON u.id = a.usuario_id"
            " ORDER BY a.id DESC LIMIT ?", (limite,)).fetchall()]


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------

def listar_usuarios(somente_ativos=True) -> list:
    sql = "SELECT * FROM usuarios"
    if somente_ativos:
        sql += " WHERE ativo = 1"
    sql += " ORDER BY nome"
    with conectar() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def buscar_usuario(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute("SELECT * FROM usuarios WHERE id = ?", (id_,)).fetchone()
        return dict(r) if r else None


def buscar_usuario_por_login(login: str) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT * FROM usuarios WHERE login = ?", (login,)).fetchone()
        return dict(r) if r else None


def campos_usuario(form) -> dict:
    return {
        "nome": (form.get("nome") or "").strip(),
        "login": (form.get("login") or "").strip().lower(),
        "perfil": form.get("perfil", "comercial"),
        "ativo": int(form.get("ativo", 1)),
    }


class ErroDeCampo(ValueError):
    def __init__(self, campo: str, mensagem: str):
        self.campo = campo
        super().__init__(mensagem)


def salvar_usuario(dados: dict, senha_hash: str | None = None,
                   id_: int | None = None) -> int:
    nome = dados.get("nome", "").strip()
    login = dados.get("login", "").strip().lower()
    perfil = dados.get("perfil", "comercial")
    ativo = int(dados.get("ativo", 1))

    if not nome:
        raise ErroDeCampo("nome", "Nome e obrigatorio.")
    if not login:
        raise ErroDeCampo("login", "Login e obrigatorio.")
    if perfil not in PERFIS:
        raise ErroDeCampo("perfil", "Perfil invalido.")

    with conectar() as conn:
        existente = conn.execute(
            "SELECT id FROM usuarios WHERE login = ? AND id != ?",
            (login, id_ or 0)).fetchone()
        if existente:
            raise ErroDeCampo("login", "Ja existe um usuario com esse login.")

        agora = formato.agora()
        if id_:
            campos = "nome=?, login=?, perfil=?, ativo=?, atualizado_em=?"
            params = [nome, login, perfil, ativo, agora, id_]
            if senha_hash:
                campos = "nome=?, login=?, perfil=?, ativo=?, senha_hash=?, atualizado_em=?"
                params = [nome, login, perfil, ativo, senha_hash, agora, id_]
            conn.execute(f"UPDATE usuarios SET {campos} WHERE id = ?", params)
            return id_
        else:
            if not senha_hash:
                raise ErroDeCampo("senha", "Senha e obrigatoria para novo usuario.")
            r = conn.execute(
                "INSERT INTO usuarios (nome, login, senha_hash, perfil, ativo, criado_em, atualizado_em)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (nome, login, senha_hash, perfil, ativo, agora, agora))
            return r.lastrowid


# ---------------------------------------------------------------------------
# Parametros
# ---------------------------------------------------------------------------

def parametro(chave: str, padrao: str = "") -> str:
    with conectar() as conn:
        r = conn.execute("SELECT valor FROM parametros WHERE chave = ?", (chave,)).fetchone()
        return r["valor"] if r else padrao


def salvar_parametro(chave: str, valor: str):
    with conectar() as conn:
        conn.execute(
            "INSERT INTO parametros (chave, valor) VALUES (?, ?)"
            " ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (chave, valor))


# ---------------------------------------------------------------------------
# Organizacao
# ---------------------------------------------------------------------------

def organizacao() -> dict:
    with conectar() as conn:
        r = conn.execute("SELECT * FROM organizacoes LIMIT 1").fetchone()
        return dict(r) if r else {}


def salvar_organizacao(dados: dict) -> int:
    org = organizacao()
    agora = formato.agora()
    with conectar() as conn:
        if org:
            conn.execute(
                "UPDATE organizacoes SET nome=?, cnpj=?, telefone=?, whatsapp=?,"
                " email=?, endereco=?, bairro=?, cidade=?, cep=?, instagram=?"
                " WHERE id = ?",
                (dados.get("nome", ""), dados.get("cnpj", ""),
                 dados.get("telefone", ""), dados.get("whatsapp", ""),
                 dados.get("email", ""), dados.get("endereco", ""),
                 dados.get("bairro", ""), dados.get("cidade", "Campo Grande"),
                 dados.get("cep", ""), dados.get("instagram", ""),
                 org["id"]))
            return org["id"]
        else:
            r = conn.execute(
                "INSERT INTO organizacoes (nome, cnpj, telefone, whatsapp,"
                " email, endereco, bairro, cidade, cep, instagram, criado_em)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dados.get("nome", "Morumbi Festas"), dados.get("cnpj", ""),
                 dados.get("telefone", ""), dados.get("whatsapp", ""),
                 dados.get("email", ""), dados.get("endereco", ""),
                 dados.get("bairro", ""), dados.get("cidade", "Campo Grande"),
                 dados.get("cep", ""), dados.get("instagram", ""), agora))
            return r.lastrowid


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def resumo_painel() -> dict:
    return {
        "vazio": True,
    }
