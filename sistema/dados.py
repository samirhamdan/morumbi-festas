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
                criado_em TEXT NOT NULL DEFAULT (datetime('now')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_clientes_cpf ON clientes(cpf_cnpj);
            CREATE INDEX IF NOT EXISTS idx_clientes_whatsapp ON clientes(whatsapp);
            CREATE INDEX IF NOT EXISTS idx_clientes_email ON clientes(email);

            CREATE TABLE IF NOT EXISTS tags_cliente (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                tag TEXT NOT NULL,
                UNIQUE(cliente_id, tag)
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
# Clientes
# ---------------------------------------------------------------------------

ORIGENS_CLIENTE = (
    "Instagram", "WhatsApp", "Indicacao", "Google", "Facebook",
    "Evento", "Loja fisica", "Outro",
)


def listar_clientes(somente_ativos=True) -> list:
    sql = "SELECT * FROM clientes"
    if somente_ativos:
        sql += " WHERE status = 'ativo'"
    sql += " ORDER BY nome"
    with conectar() as conn:
        clientes = [dict(r) for r in conn.execute(sql).fetchall()]
        for c in clientes:
            c["tags"] = _tags_do_cliente(conn, c["id"])
        return clientes


def buscar_cliente(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute("SELECT * FROM clientes WHERE id = ?", (id_,)).fetchone()
        if not r:
            return None
        c = dict(r)
        c["tags"] = _tags_do_cliente(conn, c["id"])
        return c


def _tags_do_cliente(conn, cliente_id: int) -> list:
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags_cliente WHERE cliente_id = ? ORDER BY tag",
        (cliente_id,)).fetchall()]


def campos_cliente(form) -> dict:
    return {
        "nome": (form.get("nome") or "").strip(),
        "cpf_cnpj": _limpar_doc(form.get("cpf_cnpj") or ""),
        "whatsapp": _limpar_fone(form.get("whatsapp") or ""),
        "telefone": _limpar_fone(form.get("telefone") or ""),
        "email": (form.get("email") or "").strip().lower(),
        "data_nascimento": (form.get("data_nascimento") or "").strip(),
        "endereco": (form.get("endereco") or "").strip(),
        "bairro": (form.get("bairro") or "").strip(),
        "cidade": (form.get("cidade") or "Campo Grande").strip(),
        "cep": _limpar_doc(form.get("cep") or ""),
        "instagram": (form.get("instagram") or "").strip().lstrip("@"),
        "observacoes": (form.get("observacoes") or "").strip(),
        "origem": (form.get("origem") or "").strip(),
        "status": form.get("status", "ativo"),
        "cliente_3d_id": form.get("cliente_3d_id") or None,
    }


def _limpar_doc(texto: str) -> str:
    return "".join(c for c in texto if c.isdigit())


def _limpar_fone(texto: str) -> str:
    return "".join(c for c in texto if c.isdigit() or c == "+")


def verificar_duplicidade(campo: str, valor: str, id_excluir: int | None = None) -> dict | None:
    if not valor:
        return None
    with conectar() as conn:
        sql = f"SELECT id, nome, {campo} FROM clientes WHERE {campo} = ? AND id != ?"
        r = conn.execute(sql, (valor, id_excluir or 0)).fetchone()
        return dict(r) if r else None


def salvar_cliente(dados_: dict, id_: int | None = None, tags: list | None = None) -> int:
    nome = dados_.get("nome", "").strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome e obrigatorio.")

    cpf = dados_.get("cpf_cnpj", "")
    whatsapp = dados_.get("whatsapp", "")
    email = dados_.get("email", "")

    if cpf:
        dup = verificar_duplicidade("cpf_cnpj", cpf, id_)
        if dup:
            raise ErroDeCampo("cpf_cnpj",
                              f"CPF/CNPJ ja cadastrado para {dup['nome']}.")
    if whatsapp:
        dup = verificar_duplicidade("whatsapp", whatsapp, id_)
        if dup:
            raise ErroDeCampo("whatsapp",
                              f"WhatsApp ja cadastrado para {dup['nome']}.")
    if email:
        dup = verificar_duplicidade("email", email, id_)
        if dup:
            raise ErroDeCampo("email",
                              f"E-mail ja cadastrado para {dup['nome']}.")

    agora_ = formato.agora()
    with conectar() as conn:
        if id_:
            conn.execute(
                "UPDATE clientes SET nome=?, cpf_cnpj=?, whatsapp=?, telefone=?,"
                " email=?, data_nascimento=?, endereco=?, bairro=?, cidade=?,"
                " cep=?, instagram=?, observacoes=?, origem=?, status=?,"
                " cliente_3d_id=?, atualizado_em=? WHERE id = ?",
                (nome, cpf, whatsapp, dados_.get("telefone", ""),
                 email, dados_.get("data_nascimento", ""),
                 dados_.get("endereco", ""), dados_.get("bairro", ""),
                 dados_.get("cidade", "Campo Grande"), dados_.get("cep", ""),
                 dados_.get("instagram", ""), dados_.get("observacoes", ""),
                 dados_.get("origem", ""), dados_.get("status", "ativo"),
                 dados_.get("cliente_3d_id"), agora_, id_))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO clientes (nome, cpf_cnpj, whatsapp, telefone,"
                " email, data_nascimento, endereco, bairro, cidade, cep,"
                " instagram, observacoes, origem, status, cliente_3d_id,"
                " criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (nome, cpf, whatsapp, dados_.get("telefone", ""),
                 email, dados_.get("data_nascimento", ""),
                 dados_.get("endereco", ""), dados_.get("bairro", ""),
                 dados_.get("cidade", "Campo Grande"), dados_.get("cep", ""),
                 dados_.get("instagram", ""), dados_.get("observacoes", ""),
                 dados_.get("origem", ""), dados_.get("status", "ativo"),
                 dados_.get("cliente_3d_id"), agora_, agora_))
            novo_id = r.lastrowid

        if tags is not None:
            conn.execute("DELETE FROM tags_cliente WHERE cliente_id = ?", (novo_id,))
            for tag in tags:
                tag = tag.strip()
                if tag:
                    conn.execute(
                        "INSERT OR IGNORE INTO tags_cliente (cliente_id, tag)"
                        " VALUES (?, ?)", (novo_id, tag))

        return novo_id


def link_whatsapp(numero: str) -> str:
    n = _limpar_fone(numero)
    if not n:
        return ""
    if not n.startswith("55"):
        n = "55" + n
    return f"https://wa.me/{n}"


def exportar_clientes_csv(clientes: list) -> str:
    import csv
    import io
    saida = io.StringIO()
    escritor = csv.writer(saida)
    escritor.writerow(["Nome", "WhatsApp", "Cidade", "Bairro", "Origem",
                       "Instagram", "Tags", "Status"])
    for c in clientes:
        escritor.writerow([
            c.get("nome", ""),
            c.get("whatsapp", ""),
            c.get("cidade", ""),
            c.get("bairro", ""),
            c.get("origem", ""),
            c.get("instagram", ""),
            ", ".join(c.get("tags", [])),
            c.get("status", ""),
        ])
    return saida.getvalue()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def resumo_painel() -> dict:
    with conectar() as conn:
        total = conn.execute("SELECT COUNT(*) FROM clientes WHERE status='ativo'").fetchone()[0]
    return {
        "vazio": total == 0,
        "total_clientes": total,
    }
