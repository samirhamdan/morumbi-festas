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

            CREATE TABLE IF NOT EXISTS categorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                pai_id INTEGER REFERENCES categorias(id),
                ordem INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo_sku TEXT NOT NULL UNIQUE,
                nome TEXT NOT NULL,
                categoria_id INTEGER REFERENCES categorias(id),
                descricao TEXT DEFAULT '',
                preco_locacao REAL NOT NULL DEFAULT 0,
                valor_referencia REAL NOT NULL DEFAULT 0,
                quantidade_total INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'disponivel',
                localizacao TEXT DEFAULT '',
                observacoes TEXT DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT (datetime('now')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_produtos_sku ON produtos(codigo_sku);
            CREATE INDEX IF NOT EXISTS idx_produtos_categoria ON produtos(categoria_id);

            CREATE TABLE IF NOT EXISTS fotos_produto (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                produto_id INTEGER NOT NULL REFERENCES produtos(id),
                arquivo TEXT NOT NULL,
                principal INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tags_produto (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                produto_id INTEGER NOT NULL REFERENCES produtos(id),
                tag TEXT NOT NULL,
                UNIQUE(produto_id, tag)
            );

            CREATE TABLE IF NOT EXISTS kits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                descricao TEXT DEFAULT '',
                preco REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ativo',
                criado_em TEXT NOT NULL DEFAULT (datetime('now')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS itens_kit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit_id INTEGER NOT NULL REFERENCES kits(id),
                produto_id INTEGER NOT NULL REFERENCES produtos(id),
                quantidade INTEGER NOT NULL DEFAULT 1,
                UNIQUE(kit_id, produto_id)
            );

            CREATE TABLE IF NOT EXISTS fotos_kit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit_id INTEGER NOT NULL REFERENCES kits(id),
                arquivo TEXT NOT NULL,
                principal INTEGER NOT NULL DEFAULT 0,
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
# Categorias
# ---------------------------------------------------------------------------

def listar_categorias() -> list:
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM categorias ORDER BY ordem, nome").fetchall()]


def buscar_categoria(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute("SELECT * FROM categorias WHERE id = ?", (id_,)).fetchone()
        return dict(r) if r else None


def categorias_arvore() -> list:
    cats = listar_categorias()
    pais = [c for c in cats if not c["pai_id"]]
    for p in pais:
        p["filhos"] = [c for c in cats if c["pai_id"] == p["id"]]
    return pais


def salvar_categoria(nome: str, pai_id: int | None = None,
                     id_: int | None = None) -> int:
    nome = nome.strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome da categoria e obrigatorio.")
    with conectar() as conn:
        if id_:
            conn.execute("UPDATE categorias SET nome=?, pai_id=? WHERE id=?",
                         (nome, pai_id, id_))
            return id_
        r = conn.execute(
            "INSERT INTO categorias (nome, pai_id, criado_em) VALUES (?, ?, ?)",
            (nome, pai_id, formato.agora()))
        return r.lastrowid


def excluir_categoria(id_: int):
    with conectar() as conn:
        em_uso = conn.execute(
            "SELECT COUNT(*) FROM produtos WHERE categoria_id = ?", (id_,)
        ).fetchone()[0]
        if em_uso:
            raise ErroDeCampo("nome", "Categoria em uso por produtos.")
        filhos = conn.execute(
            "SELECT COUNT(*) FROM categorias WHERE pai_id = ?", (id_,)
        ).fetchone()[0]
        if filhos:
            raise ErroDeCampo("nome", "Categoria possui subcategorias.")
        conn.execute("DELETE FROM categorias WHERE id = ?", (id_,))


def _prefixo_categoria(conn, categoria_id: int | None) -> str:
    if not categoria_id:
        return "GER"
    r = conn.execute("SELECT nome FROM categorias WHERE id = ?",
                     (categoria_id,)).fetchone()
    if not r:
        return "GER"
    nome = r["nome"].upper().replace(" ", "")
    return nome[:3] if len(nome) >= 3 else nome.ljust(3, "X")


def gerar_sku(categoria_id: int | None = None) -> str:
    with conectar() as conn:
        prefixo = _prefixo_categoria(conn, categoria_id)
        r = conn.execute(
            "SELECT COUNT(*) FROM produtos WHERE codigo_sku LIKE ?",
            (f"{prefixo}-%",)).fetchone()[0]
        seq = r + 1
        return f"{prefixo}-{seq:04d}"


# ---------------------------------------------------------------------------
# Produtos
# ---------------------------------------------------------------------------

STATUS_PRODUTO = ("disponivel", "manutencao", "inativo")


def listar_produtos(status: str | None = None) -> list:
    sql = ("SELECT p.*, c.nome AS categoria_nome"
           " FROM produtos p LEFT JOIN categorias c ON c.id = p.categoria_id")
    params = []
    if status:
        sql += " WHERE p.status = ?"
        params.append(status)
    sql += " ORDER BY p.nome"
    with conectar() as conn:
        prods = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for p in prods:
            p["tags"] = _tags_do_produto(conn, p["id"])
            p["foto_capa"] = _foto_capa(conn, p["id"])
        return prods


def buscar_produto(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT p.*, c.nome AS categoria_nome"
            " FROM produtos p LEFT JOIN categorias c ON c.id = p.categoria_id"
            " WHERE p.id = ?", (id_,)).fetchone()
        if not r:
            return None
        p = dict(r)
        p["tags"] = _tags_do_produto(conn, p["id"])
        p["fotos"] = _fotos_do_produto(conn, p["id"])
        p["foto_capa"] = _foto_capa(conn, p["id"])
        return p


def _tags_do_produto(conn, produto_id: int) -> list:
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags_produto WHERE produto_id = ? ORDER BY tag",
        (produto_id,)).fetchall()]


def _fotos_do_produto(conn, produto_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM fotos_produto WHERE produto_id = ? ORDER BY principal DESC, id",
        (produto_id,)).fetchall()]


def _foto_capa(conn, produto_id: int) -> dict | None:
    r = conn.execute(
        "SELECT * FROM fotos_produto WHERE produto_id = ? ORDER BY principal DESC, id LIMIT 1",
        (produto_id,)).fetchone()
    return dict(r) if r else None


def campos_produto(form) -> dict:
    def _float(val, padrao=0):
        try:
            return float(val)
        except (TypeError, ValueError):
            return padrao

    def _int(val, padrao=1):
        try:
            return int(val)
        except (TypeError, ValueError):
            return padrao

    cat = form.get("categoria_id") or None
    if cat:
        try:
            cat = int(cat)
        except (TypeError, ValueError):
            cat = None

    return {
        "nome": (form.get("nome") or "").strip(),
        "categoria_id": cat,
        "descricao": (form.get("descricao") or "").strip(),
        "preco_locacao": _float(form.get("preco_locacao")),
        "valor_referencia": _float(form.get("valor_referencia")),
        "quantidade_total": _int(form.get("quantidade_total")),
        "status": form.get("status", "disponivel"),
        "localizacao": (form.get("localizacao") or "").strip(),
        "observacoes": (form.get("observacoes") or "").strip(),
    }


def salvar_produto(dados_: dict, id_: int | None = None,
                   tags: list | None = None) -> int:
    nome = dados_.get("nome", "").strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome do produto e obrigatorio.")

    status = dados_.get("status", "disponivel")
    if status not in STATUS_PRODUTO:
        raise ErroDeCampo("status", "Status invalido.")

    agora_ = formato.agora()
    with conectar() as conn:
        if id_:
            prod = conn.execute("SELECT codigo_sku FROM produtos WHERE id=?",
                                (id_,)).fetchone()
            sku = prod["codigo_sku"] if prod else gerar_sku(dados_.get("categoria_id"))
            conn.execute(
                "UPDATE produtos SET nome=?, categoria_id=?, descricao=?,"
                " preco_locacao=?, valor_referencia=?, quantidade_total=?,"
                " status=?, localizacao=?, observacoes=?, atualizado_em=?"
                " WHERE id = ?",
                (nome, dados_.get("categoria_id"),
                 dados_.get("descricao", ""),
                 dados_.get("preco_locacao", 0),
                 dados_.get("valor_referencia", 0),
                 dados_.get("quantidade_total", 1),
                 status, dados_.get("localizacao", ""),
                 dados_.get("observacoes", ""), agora_, id_))
            novo_id = id_
        else:
            sku = gerar_sku(dados_.get("categoria_id"))
            r = conn.execute(
                "INSERT INTO produtos (codigo_sku, nome, categoria_id, descricao,"
                " preco_locacao, valor_referencia, quantidade_total, status,"
                " localizacao, observacoes, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (sku, nome, dados_.get("categoria_id"),
                 dados_.get("descricao", ""),
                 dados_.get("preco_locacao", 0),
                 dados_.get("valor_referencia", 0),
                 dados_.get("quantidade_total", 1),
                 status, dados_.get("localizacao", ""),
                 dados_.get("observacoes", ""), agora_, agora_))
            novo_id = r.lastrowid

        if tags is not None:
            conn.execute("DELETE FROM tags_produto WHERE produto_id = ?", (novo_id,))
            for tag in tags:
                tag = tag.strip()
                if tag:
                    conn.execute(
                        "INSERT OR IGNORE INTO tags_produto (produto_id, tag)"
                        " VALUES (?, ?)", (novo_id, tag))

        return novo_id


def disponibilidade(produto_id: int, data: str | None = None) -> int:
    p = buscar_produto(produto_id)
    if not p:
        return 0
    return p["quantidade_total"]


# ---------------------------------------------------------------------------
# Fotos de produto
# ---------------------------------------------------------------------------

def salvar_foto_produto(produto_id: int, arquivo: str, principal: bool = False) -> int:
    with conectar() as conn:
        if principal:
            conn.execute("UPDATE fotos_produto SET principal=0 WHERE produto_id=?",
                         (produto_id,))
        existentes = conn.execute(
            "SELECT COUNT(*) FROM fotos_produto WHERE produto_id=?",
            (produto_id,)).fetchone()[0]
        is_principal = 1 if (principal or existentes == 0) else 0
        r = conn.execute(
            "INSERT INTO fotos_produto (produto_id, arquivo, principal, criado_em)"
            " VALUES (?, ?, ?, ?)",
            (produto_id, arquivo, is_principal, formato.agora()))
        return r.lastrowid


def definir_foto_principal(foto_id: int, produto_id: int):
    with conectar() as conn:
        conn.execute("UPDATE fotos_produto SET principal=0 WHERE produto_id=?",
                     (produto_id,))
        conn.execute("UPDATE fotos_produto SET principal=1 WHERE id=?", (foto_id,))


def excluir_foto_produto(foto_id: int) -> dict | None:
    with conectar() as conn:
        foto = conn.execute("SELECT * FROM fotos_produto WHERE id=?",
                            (foto_id,)).fetchone()
        if not foto:
            return None
        foto = dict(foto)
        conn.execute("DELETE FROM fotos_produto WHERE id=?", (foto_id,))
        if foto["principal"]:
            outra = conn.execute(
                "SELECT id FROM fotos_produto WHERE produto_id=? ORDER BY id LIMIT 1",
                (foto["produto_id"],)).fetchone()
            if outra:
                conn.execute("UPDATE fotos_produto SET principal=1 WHERE id=?",
                             (outra["id"],))
        return foto


# ---------------------------------------------------------------------------
# Kits
# ---------------------------------------------------------------------------

STATUS_KIT = ("ativo", "inativo")


def listar_kits(status: str | None = None) -> list:
    sql = "SELECT * FROM kits"
    params = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY nome"
    with conectar() as conn:
        kits = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for k in kits:
            k["itens"] = _itens_do_kit(conn, k["id"])
            k["foto_capa"] = _foto_capa_kit(conn, k["id"])
            k["soma_produtos"] = sum(
                (i.get("preco_locacao") or 0) * i["quantidade"]
                for i in k["itens"])
        return kits


def buscar_kit(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute("SELECT * FROM kits WHERE id = ?", (id_,)).fetchone()
        if not r:
            return None
        k = dict(r)
        k["itens"] = _itens_do_kit(conn, k["id"])
        k["fotos"] = _fotos_do_kit(conn, k["id"])
        k["foto_capa"] = _foto_capa_kit(conn, k["id"])
        k["soma_produtos"] = sum(
            (i.get("preco_locacao") or 0) * i["quantidade"]
            for i in k["itens"])
        return k


def _itens_do_kit(conn, kit_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT ik.*, p.nome AS produto_nome, p.codigo_sku,"
        " p.preco_locacao, p.quantidade_total, p.status AS produto_status"
        " FROM itens_kit ik"
        " JOIN produtos p ON p.id = ik.produto_id"
        " WHERE ik.kit_id = ? ORDER BY p.nome",
        (kit_id,)).fetchall()]


def _fotos_do_kit(conn, kit_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM fotos_kit WHERE kit_id = ? ORDER BY principal DESC, id",
        (kit_id,)).fetchall()]


def _foto_capa_kit(conn, kit_id: int) -> dict | None:
    r = conn.execute(
        "SELECT * FROM fotos_kit WHERE kit_id = ? ORDER BY principal DESC, id LIMIT 1",
        (kit_id,)).fetchone()
    return dict(r) if r else None


def campos_kit(form) -> dict:
    def _float(val, padrao=0):
        try:
            return float(val)
        except (TypeError, ValueError):
            return padrao

    return {
        "nome": (form.get("nome") or "").strip(),
        "descricao": (form.get("descricao") or "").strip(),
        "preco": _float(form.get("preco")),
        "status": form.get("status", "ativo"),
    }


def salvar_kit(dados_: dict, id_: int | None = None) -> int:
    nome = dados_.get("nome", "").strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome do kit e obrigatorio.")

    status = dados_.get("status", "ativo")
    if status not in STATUS_KIT:
        raise ErroDeCampo("status", "Status invalido.")

    agora_ = formato.agora()
    with conectar() as conn:
        if id_:
            conn.execute(
                "UPDATE kits SET nome=?, descricao=?, preco=?,"
                " status=?, atualizado_em=? WHERE id = ?",
                (nome, dados_.get("descricao", ""),
                 dados_.get("preco", 0), status, agora_, id_))
            return id_
        r = conn.execute(
            "INSERT INTO kits (nome, descricao, preco, status,"
            " criado_em, atualizado_em) VALUES (?,?,?,?,?,?)",
            (nome, dados_.get("descricao", ""),
             dados_.get("preco", 0), status, agora_, agora_))
        return r.lastrowid


def adicionar_item_kit(kit_id: int, produto_id: int, quantidade: int = 1) -> int:
    if quantidade < 1:
        raise ErroDeCampo("quantidade", "Quantidade minima e 1.")
    with conectar() as conn:
        existente = conn.execute(
            "SELECT id FROM itens_kit WHERE kit_id = ? AND produto_id = ?",
            (kit_id, produto_id)).fetchone()
        if existente:
            conn.execute(
                "UPDATE itens_kit SET quantidade = ? WHERE id = ?",
                (quantidade, existente["id"]))
            return existente["id"]
        r = conn.execute(
            "INSERT INTO itens_kit (kit_id, produto_id, quantidade)"
            " VALUES (?, ?, ?)", (kit_id, produto_id, quantidade))
        return r.lastrowid


def remover_item_kit(item_id: int):
    with conectar() as conn:
        conn.execute("DELETE FROM itens_kit WHERE id = ?", (item_id,))


def disponibilidade_kit(kit_id: int, data: str | None = None) -> int:
    kit = buscar_kit(kit_id)
    if not kit or not kit["itens"]:
        return 0
    minimo = None
    for item in kit["itens"]:
        if item["produto_status"] == "manutencao":
            return 0
        if item["produto_status"] == "inativo":
            return 0
        disp_produto = disponibilidade(item["produto_id"], data)
        kits_possiveis = disp_produto // item["quantidade"] if item["quantidade"] > 0 else 0
        if minimo is None or kits_possiveis < minimo:
            minimo = kits_possiveis
    return minimo if minimo is not None else 0


# ---------------------------------------------------------------------------
# Fotos de kit
# ---------------------------------------------------------------------------

def salvar_foto_kit(kit_id: int, arquivo: str, principal: bool = False) -> int:
    with conectar() as conn:
        if principal:
            conn.execute("UPDATE fotos_kit SET principal=0 WHERE kit_id=?",
                         (kit_id,))
        existentes = conn.execute(
            "SELECT COUNT(*) FROM fotos_kit WHERE kit_id=?",
            (kit_id,)).fetchone()[0]
        is_principal = 1 if (principal or existentes == 0) else 0
        r = conn.execute(
            "INSERT INTO fotos_kit (kit_id, arquivo, principal, criado_em)"
            " VALUES (?, ?, ?, ?)",
            (kit_id, arquivo, is_principal, formato.agora()))
        return r.lastrowid


def definir_foto_principal_kit(foto_id: int, kit_id: int):
    with conectar() as conn:
        conn.execute("UPDATE fotos_kit SET principal=0 WHERE kit_id=?",
                     (kit_id,))
        conn.execute("UPDATE fotos_kit SET principal=1 WHERE id=?", (foto_id,))


def excluir_foto_kit(foto_id: int) -> dict | None:
    with conectar() as conn:
        foto = conn.execute("SELECT * FROM fotos_kit WHERE id=?",
                            (foto_id,)).fetchone()
        if not foto:
            return None
        foto = dict(foto)
        conn.execute("DELETE FROM fotos_kit WHERE id=?", (foto_id,))
        if foto["principal"]:
            outra = conn.execute(
                "SELECT id FROM fotos_kit WHERE kit_id=? ORDER BY id LIMIT 1",
                (foto["kit_id"],)).fetchone()
            if outra:
                conn.execute("UPDATE fotos_kit SET principal=1 WHERE id=?",
                             (outra["id"],))
        return foto


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def resumo_painel() -> dict:
    with conectar() as conn:
        total_clientes = conn.execute(
            "SELECT COUNT(*) FROM clientes WHERE status='ativo'").fetchone()[0]
        total_produtos = conn.execute(
            "SELECT COUNT(*) FROM produtos WHERE status != 'inativo'").fetchone()[0]
        total_kits = conn.execute(
            "SELECT COUNT(*) FROM kits WHERE status = 'ativo'").fetchone()[0]
    return {
        "vazio": total_clientes == 0 and total_produtos == 0 and total_kits == 0,
        "total_clientes": total_clientes,
        "total_produtos": total_produtos,
        "total_kits": total_kits,
    }
