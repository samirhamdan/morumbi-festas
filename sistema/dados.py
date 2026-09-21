"""Camada de dados — SQLite."""

import json
import os
import sqlite3
from datetime import date, datetime

from sistema import formato

PERFIS = ("admin", "comercial", "operacional", "gestor")

ETAPAS_LEAD = (
    "novo", "atendimento", "orcamento", "negociacao",
    "reserva", "contratado", "concluido",
)
ETAPAS_ALT_LEAD = ("perdido", "cancelado")
STATUS_ORCAMENTO = ("rascunho", "enviado", "aceito", "recusado")
STATUS_PEDIDO_COMERCIAL = ("confirmado", "entregue", "devolvido", "cancelado")
STATUS_PEDIDO_OPERACIONAL = (
    "preparacao", "separado", "montado", "entregue",
    "recolhido", "conferido", "cancelado",
)

DATA_CORTE_FINALIZADOS = "2026-09-18"

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
                publicado INTEGER NOT NULL DEFAULT 0,
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
                publicado INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS origens_lead (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
                criado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente_id INTEGER REFERENCES clientes(id),
                origem_id INTEGER REFERENCES origens_lead(id),
                interesse TEXT DEFAULT '',
                valor_estimado REAL NOT NULL DEFAULT 0,
                responsavel_id INTEGER REFERENCES usuarios(id),
                status TEXT NOT NULL DEFAULT 'novo',
                data_evento TEXT,
                observacoes TEXT DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT (datetime('now')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_cliente ON leads(cliente_id);

            CREATE TABLE IF NOT EXISTS orcamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER REFERENCES leads(id),
                cliente_id INTEGER REFERENCES clientes(id),
                desconto REAL NOT NULL DEFAULT 0,
                observacoes TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'rascunho',
                criado_em TEXT NOT NULL DEFAULT (datetime('now')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS itens_orcamento (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                orcamento_id INTEGER NOT NULL REFERENCES orcamentos(id),
                tipo TEXT NOT NULL DEFAULT 'produto',
                item_id INTEGER,
                descricao TEXT NOT NULL,
                quantidade INTEGER NOT NULL DEFAULT 1,
                preco_unitario REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pedidos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                orcamento_id INTEGER REFERENCES orcamentos(id),
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                data_evento TEXT,
                status_comercial TEXT NOT NULL DEFAULT 'confirmado',
                status_operacional TEXT NOT NULL DEFAULT 'preparacao',
                observacoes TEXT DEFAULT '',
                criado_em TEXT NOT NULL DEFAULT (datetime('now')),
                atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_pedidos_cliente ON pedidos(cliente_id);

            CREATE TABLE IF NOT EXISTS itens_pedido (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL REFERENCES pedidos(id),
                tipo TEXT NOT NULL DEFAULT 'produto',
                item_id INTEGER,
                descricao TEXT NOT NULL,
                quantidade INTEGER NOT NULL DEFAULT 1,
                preco_unitario REAL NOT NULL DEFAULT 0
            );
        """)

        # Semente: origens de lead padrao
        origens = conn.execute("SELECT COUNT(*) FROM origens_lead").fetchone()[0]
        if origens == 0:
            for nome in ("Instagram", "Facebook", "WhatsApp", "Google",
                         "Indicacao", "Site", "Recorrente"):
                conn.execute("INSERT INTO origens_lead (nome) VALUES (?)",
                             (nome,))

        # Migracao: adicionar coluna publicado em bancos existentes
        for tabela in ("produtos", "kits"):
            colunas = [r[1] for r in conn.execute(
                f"PRAGMA table_info({tabela})").fetchall()]
            if "publicado" not in colunas:
                conn.execute(
                    f"ALTER TABLE {tabela} ADD COLUMN publicado"
                    " INTEGER NOT NULL DEFAULT 0")

        # Migracao S07: colunas de reserva em pedidos
        cols_pedido = [r[1] for r in conn.execute(
            "PRAGMA table_info(pedidos)").fetchall()]
        if "data_retirada" not in cols_pedido:
            conn.execute(
                "ALTER TABLE pedidos ADD COLUMN data_retirada TEXT")
        if "data_devolucao" not in cols_pedido:
            conn.execute(
                "ALTER TABLE pedidos ADD COLUMN data_devolucao TEXT")
        if "motivo_cancelamento" not in cols_pedido:
            conn.execute(
                "ALTER TABLE pedidos ADD COLUMN motivo_cancelamento"
                " TEXT DEFAULT ''")

        # Migracao: eventos historicos importados do 3D
        tabelas = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "eventos_historico" not in tabelas:
            conn.execute("""
                CREATE TABLE eventos_historico (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cliente_id INTEGER REFERENCES clientes(id),
                    origem_id INTEGER NOT NULL,
                    origem TEXT NOT NULL DEFAULT 'Morumbi 3D',
                    data_evento TEXT,
                    descricao TEXT NOT NULL DEFAULT '',
                    observacoes TEXT DEFAULT '',
                    canal TEXT DEFAULT '',
                    valor REAL DEFAULT 0,
                    status_origem TEXT DEFAULT '',
                    criado_em TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_evt_hist_origem"
                " ON eventos_historico (origem, origem_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_evt_hist_cliente"
                " ON eventos_historico (cliente_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_evt_hist_data"
                " ON eventos_historico (data_evento)")

        # Migracao: classificacao de festas no cliente
        cols_cliente = [r[1] for r in conn.execute(
            "PRAGMA table_info(clientes)").fetchall()]
        if "total_festas" not in cols_cliente:
            conn.execute(
                "ALTER TABLE clientes ADD COLUMN"
                " total_festas INTEGER NOT NULL DEFAULT 0")
        if "classificacao" not in cols_cliente:
            conn.execute(
                "ALTER TABLE clientes ADD COLUMN"
                " classificacao TEXT NOT NULL DEFAULT 'Sem histórico'")
        if "ultima_festa" not in cols_cliente:
            conn.execute(
                "ALTER TABLE clientes ADD COLUMN ultima_festa TEXT")

        conn.execute(
            "UPDATE clientes SET classificacao = 'Sem histórico'"
            " WHERE classificacao = 'Sem historico'")
        conn.execute(
            "UPDATE clientes SET classificacao = 'Cliente VIP histórico'"
            " WHERE classificacao = 'Cliente VIP historico'")


CLASSIFICACOES_FESTA = (
    (0, "Sem histórico"),
    (1, "Cliente de 1 festa"),
    (2, "Cliente recorrente"),
    (5, "Cliente frequente"),
    (10, "Cliente VIP histórico"),
)


def classificar_festas(total: int) -> str:
    for minimo, rotulo in reversed(CLASSIFICACOES_FESTA):
        if total >= minimo:
            return rotulo
    return "Sem histórico"


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
        raise ErroDeCampo("nome", "Nome é obrigatório.")
    if not login:
        raise ErroDeCampo("login", "Login é obrigatório.")
    if perfil not in PERFIS:
        raise ErroDeCampo("perfil", "Perfil inválido.")

    with conectar() as conn:
        existente = conn.execute(
            "SELECT id FROM usuarios WHERE login = ? AND id != ?",
            (login, id_ or 0)).fetchone()
        if existente:
            raise ErroDeCampo("login", "Já existe um usuário com esse login.")

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
                raise ErroDeCampo("senha", "Senha é obrigatória para novo usuário.")
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
        raise ErroDeCampo("nome", "Nome é obrigatório.")

    cpf = dados_.get("cpf_cnpj", "")
    whatsapp = dados_.get("whatsapp", "")
    email = dados_.get("email", "")

    if cpf:
        dup = verificar_duplicidade("cpf_cnpj", cpf, id_)
        if dup:
            raise ErroDeCampo("cpf_cnpj",
                              f"CPF/CNPJ já cadastrado para {dup['nome']}.")
    if whatsapp:
        dup = verificar_duplicidade("whatsapp", whatsapp, id_)
        if dup:
            raise ErroDeCampo("whatsapp",
                              f"WhatsApp já cadastrado para {dup['nome']}.")
    if email:
        dup = verificar_duplicidade("email", email, id_)
        if dup:
            raise ErroDeCampo("email",
                              f"E-mail já cadastrado para {dup['nome']}.")

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
        raise ErroDeCampo("nome", "Nome da categoria é obrigatório.")
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
        "publicado": 1 if form.get("publicado") else 0,
        "localizacao": (form.get("localizacao") or "").strip(),
        "observacoes": (form.get("observacoes") or "").strip(),
    }


def salvar_produto(dados_: dict, id_: int | None = None,
                   tags: list | None = None) -> int:
    nome = dados_.get("nome", "").strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome do produto é obrigatório.")

    status = dados_.get("status", "disponivel")
    if status not in STATUS_PRODUTO:
        raise ErroDeCampo("status", "Status inválido.")

    agora_ = formato.agora()
    with conectar() as conn:
        if id_:
            prod = conn.execute("SELECT codigo_sku FROM produtos WHERE id=?",
                                (id_,)).fetchone()
            sku = prod["codigo_sku"] if prod else gerar_sku(dados_.get("categoria_id"))
            conn.execute(
                "UPDATE produtos SET nome=?, categoria_id=?, descricao=?,"
                " preco_locacao=?, valor_referencia=?, quantidade_total=?,"
                " status=?, publicado=?, localizacao=?, observacoes=?,"
                " atualizado_em=? WHERE id = ?",
                (nome, dados_.get("categoria_id"),
                 dados_.get("descricao", ""),
                 dados_.get("preco_locacao", 0),
                 dados_.get("valor_referencia", 0),
                 dados_.get("quantidade_total", 1),
                 status, dados_.get("publicado", 0),
                 dados_.get("localizacao", ""),
                 dados_.get("observacoes", ""), agora_, id_))
            novo_id = id_
        else:
            sku = gerar_sku(dados_.get("categoria_id"))
            r = conn.execute(
                "INSERT INTO produtos (codigo_sku, nome, categoria_id, descricao,"
                " preco_locacao, valor_referencia, quantidade_total, status,"
                " publicado, localizacao, observacoes, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sku, nome, dados_.get("categoria_id"),
                 dados_.get("descricao", ""),
                 dados_.get("preco_locacao", 0),
                 dados_.get("valor_referencia", 0),
                 dados_.get("quantidade_total", 1),
                 status, dados_.get("publicado", 0),
                 dados_.get("localizacao", ""),
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


def disponibilidade(produto_id: int, data_inicio: str | None = None,
                    data_fim: str | None = None) -> int:
    with conectar() as conn:
        r = conn.execute("SELECT quantidade_total, status FROM produtos WHERE id=?",
                         (produto_id,)).fetchone()
        if not r:
            return 0
        if r["status"] == "manutencao":
            return 0
        total = r["quantidade_total"]
        if not data_inicio or not data_fim:
            return total
        reservado = conn.execute(
            "SELECT COALESCE(SUM(ip.quantidade), 0) FROM itens_pedido ip"
            " JOIN pedidos p ON p.id = ip.pedido_id"
            " WHERE ip.tipo = 'produto' AND ip.item_id = ?"
            " AND p.status_comercial != 'cancelado'"
            " AND p.data_retirada IS NOT NULL"
            " AND p.data_devolucao IS NOT NULL"
            " AND p.data_retirada <= ? AND p.data_devolucao >= ?",
            (produto_id, data_fim, data_inicio)).fetchone()[0]
        return max(total - reservado, 0)


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
        "publicado": 1 if form.get("publicado") else 0,
    }


def salvar_kit(dados_: dict, id_: int | None = None) -> int:
    nome = dados_.get("nome", "").strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome do kit é obrigatório.")

    status = dados_.get("status", "ativo")
    if status not in STATUS_KIT:
        raise ErroDeCampo("status", "Status inválido.")

    agora_ = formato.agora()
    with conectar() as conn:
        if id_:
            conn.execute(
                "UPDATE kits SET nome=?, descricao=?, preco=?,"
                " status=?, publicado=?, atualizado_em=? WHERE id = ?",
                (nome, dados_.get("descricao", ""),
                 dados_.get("preco", 0), status,
                 dados_.get("publicado", 0), agora_, id_))
            return id_
        r = conn.execute(
            "INSERT INTO kits (nome, descricao, preco, status,"
            " publicado, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?)",
            (nome, dados_.get("descricao", ""),
             dados_.get("preco", 0), status,
             dados_.get("publicado", 0), agora_, agora_))
        return r.lastrowid


def adicionar_item_kit(kit_id: int, produto_id: int, quantidade: int = 1) -> int:
    if quantidade < 1:
        raise ErroDeCampo("quantidade", "Quantidade mínima é 1.")
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


def disponibilidade_kit(kit_id: int, data_inicio: str | None = None,
                        data_fim: str | None = None) -> int:
    kit = buscar_kit(kit_id)
    if not kit or not kit["itens"]:
        return 0
    minimo = None
    for item in kit["itens"]:
        if item["produto_status"] == "manutencao":
            return 0
        if item["produto_status"] == "inativo":
            return 0
        disp_produto = disponibilidade(item["produto_id"], data_inicio, data_fim)
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
    from datetime import date, timedelta
    hoje = date.today().isoformat()
    amanha = (date.today() + timedelta(days=1)).isoformat()
    proximos_7 = (date.today() + timedelta(days=7)).isoformat()

    with conectar() as conn:
        total_clientes = conn.execute(
            "SELECT COUNT(*) FROM clientes WHERE status='ativo'").fetchone()[0]
        total_produtos = conn.execute(
            "SELECT COUNT(*) FROM produtos WHERE status != 'inativo'").fetchone()[0]
        total_kits = conn.execute(
            "SELECT COUNT(*) FROM kits WHERE status = 'ativo'").fetchone()[0]
        total_leads = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE status NOT IN ('perdido','cancelado','concluido')"
        ).fetchone()[0]
        total_orcamentos = conn.execute(
            "SELECT COUNT(*) FROM orcamentos WHERE status IN ('rascunho','enviado')"
        ).fetchone()[0]
        total_pedidos = conn.execute(
            "SELECT COUNT(*) FROM pedidos WHERE status_comercial != 'cancelado'"
        ).fetchone()[0]
        pendencias_op = conn.execute(
            "SELECT COUNT(*) FROM pedidos"
            " WHERE status_comercial != 'cancelado'"
            " AND status_operacional IN ('preparacao','separado','montado')"
        ).fetchone()[0]

        retiradas_hoje = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_retirada, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            " WHERE p.data_retirada=? AND p.status_comercial != 'cancelado'"
            " ORDER BY c.nome", (hoje,)).fetchall()]

        devolucoes_hoje = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_devolucao, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            " WHERE p.data_devolucao=? AND p.status_comercial != 'cancelado'"
            " ORDER BY c.nome", (hoje,)).fetchall()]

        devolucoes_atrasadas = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_devolucao, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            " WHERE p.data_devolucao < ? AND p.status_comercial != 'cancelado'"
            " AND p.status_operacional NOT IN ('recolhido','conferido','cancelado')"
            " ORDER BY p.data_devolucao", (hoje,)).fetchall()]

        orcamentos_sem_retorno = [dict(r) for r in conn.execute(
            "SELECT o.id, o.criado_em, c.nome AS cliente_nome"
            " FROM orcamentos o LEFT JOIN clientes c ON c.id=o.cliente_id"
            " WHERE o.status='enviado'"
            " ORDER BY o.criado_em", ).fetchall()]

        proximos_eventos = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_evento, p.data_retirada, p.data_devolucao,"
            " p.status_comercial, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            " WHERE p.status_comercial != 'cancelado'"
            " AND (p.data_evento BETWEEN ? AND ?"
            "      OR p.data_retirada BETWEEN ? AND ?"
            "      OR p.data_devolucao BETWEEN ? AND ?)"
            " ORDER BY COALESCE(p.data_evento, p.data_retirada, p.data_devolucao)",
            (amanha, proximos_7, amanha, proximos_7,
             amanha, proximos_7)).fetchall()]

    return {
        "vazio": total_clientes == 0 and total_produtos == 0 and total_kits == 0,
        "total_clientes": total_clientes,
        "total_produtos": total_produtos,
        "total_kits": total_kits,
        "total_leads": total_leads,
        "total_orcamentos": total_orcamentos,
        "total_pedidos": total_pedidos,
        "pendencias_op": pendencias_op,
        "retiradas_hoje": retiradas_hoje,
        "devolucoes_hoje": devolucoes_hoje,
        "devolucoes_atrasadas": devolucoes_atrasadas,
        "orcamentos_sem_retorno": orcamentos_sem_retorno,
        "proximos_eventos": proximos_eventos,
    }


def faturamento_mensal(ano: int, mes: int) -> dict:
    data_inicio = f"{ano}-{mes:02d}-01"
    if mes == 12:
        data_fim = f"{ano + 1}-01-01"
    else:
        data_fim = f"{ano}-{mes + 1:02d}-01"
    with conectar() as conn:
        rows = conn.execute(
            "SELECT p.id, p.cliente_id, p.data_evento, p.data_retirada,"
            " p.data_devolucao, p.status_comercial, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            " WHERE p.status_comercial='devolvido'"
            " AND p.data_devolucao >= ? AND p.data_devolucao < ?"
            " ORDER BY p.data_devolucao",
            (data_inicio, data_fim)).fetchall()
        pedidos = []
        total = 0.0
        for r in rows:
            ped = dict(r)
            soma = conn.execute(
                "SELECT COALESCE(SUM(quantidade * preco_unitario), 0)"
                " FROM itens_pedido WHERE pedido_id=?",
                (ped["id"],)).fetchone()[0]
            ped["total"] = soma
            total += soma
            pedidos.append(ped)
    return {"total": total, "pedidos": pedidos, "quantidade": len(pedidos)}


# ---------------------------------------------------------------------------
# Catalogo publico
# ---------------------------------------------------------------------------

def catalogo_produtos(categoria_id: int | None = None,
                      busca: str | None = None) -> list:
    sql = ("SELECT p.*, c.nome AS categoria_nome"
           " FROM produtos p LEFT JOIN categorias c ON c.id = p.categoria_id"
           " WHERE p.status = 'disponivel' AND p.publicado = 1")
    params: list = []
    if categoria_id:
        sql += " AND p.categoria_id = ?"
        params.append(categoria_id)
    sql += " ORDER BY p.nome"
    with conectar() as conn:
        prods = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for p in prods:
            p["foto_capa"] = _foto_capa(conn, p["id"])
            p["disponibilidade"] = p["quantidade_total"]
        if busca:
            from sistema.listas import filtrar, BUSCA_PRODUTOS
            prods = filtrar(prods, busca, BUSCA_PRODUTOS)
        return prods


def catalogo_kits(busca: str | None = None) -> list:
    sql = "SELECT * FROM kits WHERE status = 'ativo' AND publicado = 1 ORDER BY nome"
    with conectar() as conn:
        kits = [dict(r) for r in conn.execute(sql).fetchall()]
        for k in kits:
            k["itens"] = _itens_do_kit(conn, k["id"])
            k["foto_capa"] = _foto_capa_kit(conn, k["id"])
            k["soma_produtos"] = sum(
                (i.get("preco_locacao") or 0) * i["quantidade"]
                for i in k["itens"])
        if busca:
            from sistema.listas import filtrar, BUSCA_KITS
            kits = filtrar(kits, busca, BUSCA_KITS)
        return kits


def produto_publico(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT p.*, c.nome AS categoria_nome"
            " FROM produtos p LEFT JOIN categorias c ON c.id = p.categoria_id"
            " WHERE p.id = ? AND p.status = 'disponivel' AND p.publicado = 1",
            (id_,)).fetchone()
        if not r:
            return None
        p = dict(r)
        p["fotos"] = _fotos_do_produto(conn, p["id"])
        p["foto_capa"] = _foto_capa(conn, p["id"])
        p["tags"] = _tags_do_produto(conn, p["id"])
        p["disponibilidade"] = p["quantidade_total"]
        return p


def kit_publico(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT * FROM kits WHERE id = ? AND status = 'ativo' AND publicado = 1",
            (id_,)).fetchone()
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


# ---------------------------------------------------------------------------
# Origens de lead
# ---------------------------------------------------------------------------

def listar_origens() -> list:
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM origens_lead ORDER BY nome").fetchall()]


def salvar_origem(nome: str, id_: int | None = None) -> int:
    nome = nome.strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome da origem é obrigatório.")
    with conectar() as conn:
        dup = conn.execute(
            "SELECT id FROM origens_lead WHERE nome = ? AND id != ?",
            (nome, id_ or 0)).fetchone()
        if dup:
            raise ErroDeCampo("nome", "Já existe uma origem com este nome.")
        if id_:
            conn.execute("UPDATE origens_lead SET nome=? WHERE id=?",
                         (nome, id_))
            return id_
        r = conn.execute("INSERT INTO origens_lead (nome) VALUES (?)", (nome,))
        return r.lastrowid


def excluir_origem(id_: int):
    with conectar() as conn:
        em_uso = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE origem_id = ?",
            (id_,)).fetchone()[0]
        if em_uso:
            raise ValueError("Origem em uso por leads, não pode ser excluída.")
        conn.execute("DELETE FROM origens_lead WHERE id = ?", (id_,))


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def _enriquecer_lead(conn, lead: dict) -> dict:
    if lead.get("cliente_id"):
        cli = conn.execute("SELECT nome FROM clientes WHERE id=?",
                           (lead["cliente_id"],)).fetchone()
        lead["cliente_nome"] = cli["nome"] if cli else ""
    else:
        lead["cliente_nome"] = ""
    if lead.get("origem_id"):
        ori = conn.execute("SELECT nome FROM origens_lead WHERE id=?",
                           (lead["origem_id"],)).fetchone()
        lead["origem_nome"] = ori["nome"] if ori else ""
    else:
        lead["origem_nome"] = ""
    if lead.get("responsavel_id"):
        resp = conn.execute("SELECT nome FROM usuarios WHERE id=?",
                            (lead["responsavel_id"],)).fetchone()
        lead["responsavel_nome"] = resp["nome"] if resp else ""
    else:
        lead["responsavel_nome"] = ""
    return lead


def listar_leads(status: str | None = None) -> list:
    sql = "SELECT * FROM leads"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY atualizado_em DESC"
    with conectar() as conn:
        leads = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for ld in leads:
            _enriquecer_lead(conn, ld)
        return leads


def leads_por_etapa() -> dict:
    resultado: dict = {e: [] for e in ETAPAS_LEAD}
    resultado["perdido"] = []
    resultado["cancelado"] = []
    with conectar() as conn:
        rows = conn.execute(
            "SELECT * FROM leads ORDER BY atualizado_em DESC").fetchall()
        for r in rows:
            ld = dict(r)
            _enriquecer_lead(conn, ld)
            if ld["status"] in resultado:
                resultado[ld["status"]].append(ld)
    return resultado


def buscar_lead(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute("SELECT * FROM leads WHERE id = ?", (id_,)).fetchone()
        if not r:
            return None
        ld = dict(r)
        _enriquecer_lead(conn, ld)
        return ld


def campos_lead(form) -> dict:
    return {
        "cliente_id": int(form.get("cliente_id") or 0) or None,
        "origem_id": int(form.get("origem_id") or 0) or None,
        "interesse": (form.get("interesse") or "").strip(),
        "valor_estimado": float(form.get("valor_estimado") or 0),
        "responsavel_id": int(form.get("responsavel_id") or 0) or None,
        "status": form.get("status", "novo"),
        "data_evento": (form.get("data_evento") or "").strip() or None,
        "observacoes": (form.get("observacoes") or "").strip(),
    }


def salvar_lead(dados_: dict, id_: int | None = None) -> int:
    if not dados_.get("cliente_id"):
        raise ErroDeCampo("cliente_id", "Cliente é obrigatório.")

    status = dados_.get("status", "novo")
    todos = list(ETAPAS_LEAD) + list(ETAPAS_ALT_LEAD)
    if status not in todos:
        raise ErroDeCampo("status", "Status inválido.")

    agora_ = formato.agora()
    with conectar() as conn:
        if id_:
            conn.execute(
                "UPDATE leads SET cliente_id=?, origem_id=?, interesse=?,"
                " valor_estimado=?, responsavel_id=?, status=?,"
                " data_evento=?, observacoes=?, atualizado_em=?"
                " WHERE id=?",
                (dados_["cliente_id"], dados_.get("origem_id"),
                 dados_.get("interesse", ""), dados_.get("valor_estimado", 0),
                 dados_.get("responsavel_id"), status,
                 dados_.get("data_evento"), dados_.get("observacoes", ""),
                 agora_, id_))
            return id_
        r = conn.execute(
            "INSERT INTO leads (cliente_id, origem_id, interesse,"
            " valor_estimado, responsavel_id, status, data_evento,"
            " observacoes, criado_em, atualizado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (dados_["cliente_id"], dados_.get("origem_id"),
             dados_.get("interesse", ""), dados_.get("valor_estimado", 0),
             dados_.get("responsavel_id"), status,
             dados_.get("data_evento"), dados_.get("observacoes", ""),
             agora_, agora_))
        return r.lastrowid


def mover_lead(id_: int, novo_status: str):
    todos = list(ETAPAS_LEAD) + list(ETAPAS_ALT_LEAD)
    if novo_status not in todos:
        raise ValueError("Status inválido.")
    agora_ = formato.agora()
    with conectar() as conn:
        conn.execute(
            "UPDATE leads SET status=?, atualizado_em=? WHERE id=?",
            (novo_status, agora_, id_))


def contadores_lead() -> dict:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as qtd FROM leads GROUP BY status"
        ).fetchall()
        return {r["status"]: r["qtd"] for r in rows}


# ---------------------------------------------------------------------------
# Orcamentos
# ---------------------------------------------------------------------------

def listar_orcamentos(status: str | None = None) -> list:
    sql = ("SELECT o.*, c.nome AS cliente_nome FROM orcamentos o"
           " LEFT JOIN clientes c ON c.id = o.cliente_id")
    params: list = []
    if status:
        sql += " WHERE o.status = ?"
        params.append(status)
    sql += " ORDER BY o.atualizado_em DESC"
    with conectar() as conn:
        orcs = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for orc in orcs:
            orc["itens"] = _itens_orcamento(conn, orc["id"])
            orc["subtotal"] = sum(
                i["quantidade"] * i["preco_unitario"] for i in orc["itens"])
            orc["total"] = max(orc["subtotal"] - (orc["desconto"] or 0), 0)
        return orcs


def _itens_orcamento(conn, orcamento_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM itens_orcamento WHERE orcamento_id = ? ORDER BY id",
        (orcamento_id,)).fetchall()]


def buscar_orcamento(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT o.*, c.nome AS cliente_nome FROM orcamentos o"
            " LEFT JOIN clientes c ON c.id = o.cliente_id"
            " WHERE o.id = ?", (id_,)).fetchone()
        if not r:
            return None
        orc = dict(r)
        orc["itens"] = _itens_orcamento(conn, orc["id"])
        orc["subtotal"] = sum(
            i["quantidade"] * i["preco_unitario"] for i in orc["itens"])
        orc["total"] = max(orc["subtotal"] - (orc["desconto"] or 0), 0)
        if orc.get("lead_id"):
            ld = conn.execute("SELECT interesse FROM leads WHERE id=?",
                              (orc["lead_id"],)).fetchone()
            orc["lead_interesse"] = ld["interesse"] if ld else ""
        return orc


def salvar_orcamento(dados_: dict, itens: list,
                     id_: int | None = None) -> int:
    if not dados_.get("cliente_id"):
        raise ErroDeCampo("cliente_id", "Cliente é obrigatório.")

    status = dados_.get("status", "rascunho")
    if status not in STATUS_ORCAMENTO:
        raise ErroDeCampo("status", "Status inválido.")

    desconto = float(dados_.get("desconto") or 0)
    if desconto < 0:
        raise ErroDeCampo("desconto", "Desconto não pode ser negativo.")

    agora_ = formato.agora()
    with conectar() as conn:
        if id_:
            conn.execute(
                "UPDATE orcamentos SET lead_id=?, cliente_id=?, desconto=?,"
                " observacoes=?, status=?, atualizado_em=? WHERE id=?",
                (dados_.get("lead_id"), dados_["cliente_id"], desconto,
                 dados_.get("observacoes", ""), status, agora_, id_))
            conn.execute("DELETE FROM itens_orcamento WHERE orcamento_id=?",
                         (id_,))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO orcamentos (lead_id, cliente_id, desconto,"
                " observacoes, status, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?)",
                (dados_.get("lead_id"), dados_["cliente_id"], desconto,
                 dados_.get("observacoes", ""), status, agora_, agora_))
            novo_id = r.lastrowid
        for item in itens:
            if not item.get("descricao", "").strip():
                continue
            conn.execute(
                "INSERT INTO itens_orcamento (orcamento_id, tipo, item_id,"
                " descricao, quantidade, preco_unitario)"
                " VALUES (?,?,?,?,?,?)",
                (novo_id, item.get("tipo", "produto"),
                 item.get("item_id"), item["descricao"].strip(),
                 int(item.get("quantidade") or 1),
                 float(item.get("preco_unitario") or 0)))
        return novo_id


def total_orcamento(id_: int) -> float:
    orc = buscar_orcamento(id_)
    if not orc:
        return 0
    return orc["total"]


# ---------------------------------------------------------------------------
# Pedidos
# ---------------------------------------------------------------------------

def listar_pedidos(status_comercial: str | None = None,
                   status_operacional: str | None = None,
                   data_inicio: str | None = None,
                   data_fim: str | None = None) -> list:
    sql = ("SELECT p.*, c.nome AS cliente_nome FROM pedidos p"
           " LEFT JOIN clientes c ON c.id = p.cliente_id")
    conds: list[str] = []
    params: list = []
    if status_comercial:
        conds.append("p.status_comercial = ?")
        params.append(status_comercial)
    if status_operacional:
        conds.append("p.status_operacional = ?")
        params.append(status_operacional)
    if data_inicio:
        conds.append("COALESCE(p.data_evento, p.data_retirada, p.criado_em) >= ?")
        params.append(data_inicio)
    if data_fim:
        conds.append("COALESCE(p.data_evento, p.data_retirada, p.criado_em) <= ?")
        params.append(data_fim)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY p.criado_em DESC"
    with conectar() as conn:
        peds = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for ped in peds:
            ped["itens"] = [dict(r) for r in conn.execute(
                "SELECT * FROM itens_pedido WHERE pedido_id=? ORDER BY id",
                (ped["id"],)).fetchall()]
            ped["total"] = sum(
                i["quantidade"] * i["preco_unitario"] for i in ped["itens"])
        return peds


def buscar_pedido_festas(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT p.*, c.nome AS cliente_nome FROM pedidos p"
            " LEFT JOIN clientes c ON c.id = p.cliente_id"
            " WHERE p.id = ?", (id_,)).fetchone()
        if not r:
            return None
        ped = dict(r)
        ped["itens"] = [dict(r) for r in conn.execute(
            "SELECT * FROM itens_pedido WHERE pedido_id=? ORDER BY id",
            (ped["id"],)).fetchall()]
        ped["total"] = sum(
            i["quantidade"] * i["preco_unitario"] for i in ped["itens"])
        return ped


def converter_orcamento_em_pedido(orcamento_id: int) -> int:
    orc = buscar_orcamento(orcamento_id)
    if not orc:
        raise ValueError("Orçamento não encontrado.")
    if orc["status"] == "recusado":
        raise ValueError("Orçamento recusado não pode ser convertido.")

    agora_ = formato.agora()
    with conectar() as conn:
        r = conn.execute(
            "INSERT INTO pedidos (orcamento_id, cliente_id, data_evento,"
            " observacoes, criado_em, atualizado_em) VALUES (?,?,?,?,?,?)",
            (orcamento_id, orc["cliente_id"], None,
             orc.get("observacoes", ""), agora_, agora_))
        pedido_id = r.lastrowid
        for item in orc["itens"]:
            conn.execute(
                "INSERT INTO itens_pedido (pedido_id, tipo, item_id,"
                " descricao, quantidade, preco_unitario)"
                " VALUES (?,?,?,?,?,?)",
                (pedido_id, item["tipo"], item.get("item_id"),
                 item["descricao"], item["quantidade"],
                 item["preco_unitario"]))
        conn.execute(
            "UPDATE orcamentos SET status='aceito', atualizado_em=?"
            " WHERE id=?", (agora_, orcamento_id))
        if orc.get("lead_id"):
            conn.execute(
                "UPDATE leads SET status='contratado', atualizado_em=?"
                " WHERE id=?", (agora_, orc["lead_id"]))
        return pedido_id


def campos_pedido_festas(form) -> dict:
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "cliente_id": _int(form.get("cliente_id")),
        "data_evento": (form.get("data_evento") or "").strip() or None,
        "data_retirada": (form.get("data_retirada") or "").strip() or None,
        "data_devolucao": (form.get("data_devolucao") or "").strip() or None,
        "status_comercial": form.get("status_comercial", "confirmado"),
        "status_operacional": form.get("status_operacional", "preparacao"),
        "observacoes": (form.get("observacoes") or "").strip(),
    }


def _verificar_disponibilidade_itens(conn, itens: list, data_retirada: str,
                                     data_devolucao: str,
                                     pedido_id: int | None = None):
    for item in itens:
        if item.get("tipo") != "produto" or not item.get("item_id"):
            continue
        pid = item["item_id"]
        r = conn.execute(
            "SELECT quantidade_total, status, nome FROM produtos WHERE id=?",
            (pid,)).fetchone()
        if not r:
            continue
        if r["status"] == "manutencao":
            raise ErroDeCampo(
                "item_descricao_0",
                f"Produto '{r['nome']}' esta em manutencao.")
        total = r["quantidade_total"]
        sql = (
            "SELECT COALESCE(SUM(ip.quantidade), 0) FROM itens_pedido ip"
            " JOIN pedidos p ON p.id = ip.pedido_id"
            " WHERE ip.tipo = 'produto' AND ip.item_id = ?"
            " AND p.status_comercial != 'cancelado'"
            " AND p.data_retirada IS NOT NULL"
            " AND p.data_devolucao IS NOT NULL"
            " AND p.data_retirada <= ? AND p.data_devolucao >= ?")
        params: list = [pid, data_devolucao, data_retirada]
        if pedido_id:
            sql += " AND p.id != ?"
            params.append(pedido_id)
        reservado = conn.execute(sql, params).fetchone()[0]
        livre = total - reservado
        qtd = int(item.get("quantidade") or 1)
        if qtd > livre:
            raise ErroDeCampo(
                "item_descricao_0",
                f"Quantidade insuficiente para '{r['nome']}' nesta data."
                f" Disponivel: {livre}, solicitado: {qtd}.")


def salvar_pedido_festas(dados_: dict, itens: list,
                         id_: int | None = None) -> int:
    if not dados_.get("cliente_id"):
        raise ErroDeCampo("cliente_id", "Cliente é obrigatório.")

    sc = dados_.get("status_comercial", "confirmado")
    if sc not in STATUS_PEDIDO_COMERCIAL:
        raise ErroDeCampo("status_comercial", "Status comercial inválido.")

    so = dados_.get("status_operacional", "preparacao")
    if so not in STATUS_PEDIDO_OPERACIONAL:
        raise ErroDeCampo("status_operacional", "Status operacional inválido.")

    agora_ = formato.agora()
    data_ret = dados_.get("data_retirada")
    data_dev = dados_.get("data_devolucao")
    if data_ret and data_dev and data_ret > data_dev:
        raise ErroDeCampo("data_devolucao",
                          "Data de devolução deve ser posterior à retirada.")

    with conectar() as conn:
        if data_ret and data_dev and sc != "cancelado":
            _verificar_disponibilidade_itens(
                conn, itens, data_ret, data_dev, id_)

        if id_:
            conn.execute(
                "UPDATE pedidos SET cliente_id=?, data_evento=?,"
                " data_retirada=?, data_devolucao=?,"
                " status_comercial=?, status_operacional=?,"
                " observacoes=?, atualizado_em=? WHERE id=?",
                (dados_["cliente_id"], dados_.get("data_evento"),
                 data_ret, data_dev, sc, so,
                 dados_.get("observacoes", ""), agora_, id_))
            conn.execute("DELETE FROM itens_pedido WHERE pedido_id=?", (id_,))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO pedidos (cliente_id, data_evento,"
                " data_retirada, data_devolucao,"
                " status_comercial, status_operacional,"
                " observacoes, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (dados_["cliente_id"], dados_.get("data_evento"),
                 data_ret, data_dev, sc, so,
                 dados_.get("observacoes", ""), agora_, agora_))
            novo_id = r.lastrowid

        for item in itens:
            if not item.get("descricao", "").strip():
                continue
            conn.execute(
                "INSERT INTO itens_pedido (pedido_id, tipo, item_id,"
                " descricao, quantidade, preco_unitario)"
                " VALUES (?,?,?,?,?,?)",
                (novo_id, item.get("tipo", "produto"),
                 item.get("item_id"), item["descricao"].strip(),
                 int(item.get("quantidade") or 1),
                 float(item.get("preco_unitario") or 0)))

    if sc in ("entregue", "devolvido", "cancelado"):
        atualizar_classificacao_cliente(int(dados_["cliente_id"]))
    return novo_id


FLUXO_OPERACIONAL = {
    "preparacao": "separado",
    "separado": "montado",
    "montado": "entregue",
    "entregue": "recolhido",
    "recolhido": "conferido",
}


def listar_pedidos_operacional(status_operacional: str | None = None,
                               busca: str | None = None) -> list:
    sql = ("SELECT p.*, c.nome AS cliente_nome FROM pedidos p"
           " LEFT JOIN clientes c ON c.id = p.cliente_id"
           " WHERE p.status_comercial != 'cancelado'")
    params: list = []
    if status_operacional:
        sql += " AND p.status_operacional = ?"
        params.append(status_operacional)
    sql += " ORDER BY COALESCE(p.data_retirada, p.data_evento, p.criado_em)"
    with conectar() as conn:
        peds = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for ped in peds:
            ped["itens"] = [dict(r) for r in conn.execute(
                "SELECT * FROM itens_pedido WHERE pedido_id=? ORDER BY id",
                (ped["id"],)).fetchall()]
            ped["total"] = sum(
                i["quantidade"] * i["preco_unitario"] for i in ped["itens"])
        if busca:
            b = busca.lower()
            peds = [p for p in peds
                    if b in (p.get("cliente_nome") or "").lower()
                    or b in str(p.get("id", ""))]
        return peds


def avancar_status_operacional(pedido_id: int, observacao: str = "") -> str:
    agora_ = formato.agora()
    with conectar() as conn:
        ped = conn.execute(
            "SELECT status_comercial, status_operacional FROM pedidos WHERE id=?",
            (pedido_id,)).fetchone()
        if not ped:
            raise ValueError("Pedido não encontrado.")
        if ped["status_comercial"] == "cancelado":
            raise ValueError("Pedido cancelado não pode avançar.")
        atual = ped["status_operacional"]
        if atual not in FLUXO_OPERACIONAL:
            raise ValueError(f"Status '{atual}' não pode avançar.")
        proximo = FLUXO_OPERACIONAL[atual]
        conn.execute(
            "UPDATE pedidos SET status_operacional=?, atualizado_em=? WHERE id=?",
            (proximo, agora_, pedido_id))
        if observacao:
            conn.execute(
                "INSERT INTO audit_log (usuario_id, tipo, descricao, dados, criado_em)"
                " VALUES (NULL, 'operacao', ?, ?, ?)",
                (f"Pedido #{pedido_id}: {atual} -> {proximo}",
                 json.dumps({"observacao": observacao}), agora_))
        return proximo


def cancelar_pedido(id_: int, motivo: str = ""):
    agora_ = formato.agora()
    with conectar() as conn:
        ped = conn.execute("SELECT status_comercial FROM pedidos WHERE id=?",
                           (id_,)).fetchone()
        if not ped:
            raise ValueError("Pedido nao encontrado.")
        if ped["status_comercial"] == "cancelado":
            raise ValueError("Pedido ja esta cancelado.")
        conn.execute(
            "UPDATE pedidos SET status_comercial='cancelado',"
            " status_operacional='cancelado',"
            " motivo_cancelamento=?, atualizado_em=? WHERE id=?",
            (motivo, agora_, id_))


def eventos_agenda(data_inicio: str, data_fim: str,
                    tipo: str | None = None,
                    status_comercial: str | None = None) -> list:
    sql = ("SELECT p.id, p.cliente_id, p.data_evento, p.data_retirada,"
           " p.data_devolucao, p.status_comercial, p.status_operacional,"
           " p.observacoes, c.nome AS cliente_nome"
           " FROM pedidos p LEFT JOIN clientes c ON c.id = p.cliente_id"
           " WHERE p.status_comercial != 'cancelado'")
    params: list = []

    if status_comercial:
        sql += " AND p.status_comercial = ?"
        params.append(status_comercial)

    partes_data: list[str] = []
    if tipo == "evento":
        partes_data.append(
            "(p.data_evento IS NOT NULL"
            " AND p.data_evento >= ? AND p.data_evento <= ?)")
        params += [data_inicio, data_fim]
    elif tipo == "retirada":
        partes_data.append(
            "(p.data_retirada IS NOT NULL"
            " AND p.data_retirada >= ? AND p.data_retirada <= ?)")
        params += [data_inicio, data_fim]
    elif tipo == "devolucao":
        partes_data.append(
            "(p.data_devolucao IS NOT NULL"
            " AND p.data_devolucao >= ? AND p.data_devolucao <= ?)")
        params += [data_inicio, data_fim]
    else:
        partes_data.append(
            "(p.data_evento IS NOT NULL"
            " AND p.data_evento >= ? AND p.data_evento <= ?)")
        partes_data.append(
            "(p.data_retirada IS NOT NULL"
            " AND p.data_retirada >= ? AND p.data_retirada <= ?)")
        partes_data.append(
            "(p.data_devolucao IS NOT NULL"
            " AND p.data_devolucao >= ? AND p.data_devolucao <= ?)")
        params += [data_inicio, data_fim] * 3

    sql += " AND (" + " OR ".join(partes_data) + ")"
    sql += " ORDER BY COALESCE(p.data_evento, p.data_retirada, p.data_devolucao)"

    with conectar() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def listar_eventos_historico(origem: str | None = None,
                             status: str | None = None,
                             data_inicio: str | None = None,
                             data_fim: str | None = None) -> list:
    sql = ("SELECT h.id, h.cliente_id, h.data_evento, h.descricao,"
           " h.observacoes, h.canal, h.valor, h.status_origem,"
           " h.origem, h.origem_id, h.criado_em,"
           " c.nome AS cliente_nome"
           " FROM eventos_historico h"
           " LEFT JOIN clientes c ON c.id = h.cliente_id")
    conds: list[str] = []
    params: list = []
    if origem:
        conds.append("h.origem = ?")
        params.append(origem)
    if status:
        conds.append("h.status_origem = ?")
        params.append(status)
    if data_inicio:
        conds.append("h.data_evento >= ?")
        params.append(data_inicio)
    if data_fim:
        conds.append("h.data_evento <= ?")
        params.append(data_fim)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY h.data_evento DESC"
    with conectar() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def eventos_historico(data_inicio: str, data_fim: str) -> list:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT h.id, h.cliente_id, h.data_evento, h.descricao,"
            " h.observacoes, h.canal, h.valor, h.status_origem,"
            " h.origem, h.origem_id, c.nome AS cliente_nome"
            " FROM eventos_historico h"
            " LEFT JOIN clientes c ON c.id = h.cliente_id"
            " WHERE h.data_evento >= ? AND h.data_evento <= ?"
            " ORDER BY h.data_evento",
            (data_inicio, data_fim)).fetchall()
    return [dict(r) for r in rows]


def eventos_historico_cliente(cliente_id: int) -> list:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT id, data_evento, descricao, observacoes, canal, valor,"
            " status_origem, origem, origem_id"
            " FROM eventos_historico WHERE cliente_id = ?"
            " ORDER BY data_evento DESC",
            (cliente_id,)).fetchall()
    return [dict(r) for r in rows]


def salvar_evento_historico(dados_evt: dict) -> int:
    with conectar() as conn:
        cur = conn.execute(
            "INSERT INTO eventos_historico"
            " (cliente_id, origem_id, origem, data_evento, descricao,"
            "  observacoes, canal, valor, status_origem)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (dados_evt.get("cliente_id"),
             dados_evt["origem_id"],
             dados_evt.get("origem", "Morumbi 3D"),
             dados_evt.get("data_evento"),
             dados_evt.get("descricao", ""),
             dados_evt.get("observacoes", ""),
             dados_evt.get("canal", ""),
             dados_evt.get("valor", 0),
             dados_evt.get("status_origem", "")))
        return cur.lastrowid


def pedidos_cliente(cliente_id: int) -> list:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT p.id, p.data_evento, p.data_retirada, p.data_devolucao,"
            " p.status_comercial, p.status_operacional, p.observacoes,"
            " COALESCE(SUM(i.quantidade * i.preco_unitario), 0) AS valor_total"
            " FROM pedidos p"
            " LEFT JOIN itens_pedido i ON i.pedido_id = p.id"
            " WHERE p.cliente_id = ?"
            " GROUP BY p.id"
            " ORDER BY p.data_evento DESC",
            (cliente_id,)).fetchall()
    return [dict(r) for r in rows]


def atualizar_classificacao_cliente(cliente_id: int):
    with conectar() as conn:
        r1 = conn.execute(
            "SELECT COUNT(*) FROM eventos_historico"
            " WHERE cliente_id = ? AND status_origem = 'entregue'",
            (cliente_id,)).fetchone()
        r2 = conn.execute(
            "SELECT COUNT(*) FROM pedidos"
            " WHERE cliente_id = ?"
            " AND status_comercial IN ('entregue', 'devolvido', 'finalizado')",
            (cliente_id,)).fetchone()
        total = r1[0] + r2[0]

        r3 = conn.execute(
            "SELECT MAX(data_evento) FROM ("
            "  SELECT data_evento FROM eventos_historico"
            "  WHERE cliente_id = ? AND status_origem = 'entregue'"
            "  UNION ALL"
            "  SELECT data_evento FROM pedidos"
            "  WHERE cliente_id = ?"
            "  AND status_comercial IN ('entregue', 'devolvido', 'finalizado')"
            ")", (cliente_id, cliente_id)).fetchone()
        ultima = r3[0]

        conn.execute(
            "UPDATE clientes SET total_festas = ?, classificacao = ?,"
            " ultima_festa = ? WHERE id = ?",
            (total, classificar_festas(total), ultima, cliente_id))


def atualizar_todas_classificacoes():
    with conectar() as conn:
        clientes = conn.execute(
            "SELECT DISTINCT cliente_id FROM ("
            "  SELECT cliente_id FROM eventos_historico"
            "  WHERE cliente_id IS NOT NULL"
            "  UNION"
            "  SELECT cliente_id FROM pedidos"
            "  WHERE cliente_id IS NOT NULL"
            "  AND status_comercial IN ('entregue', 'devolvido', 'finalizado')"
            ")").fetchall()
    for row in clientes:
        atualizar_classificacao_cliente(row[0])


def disponibilidade_calendario(produto_id: int, ano: int, mes: int) -> list:
    import calendar
    _, ultimo_dia = calendar.monthrange(ano, mes)
    resultado = []
    with conectar() as conn:
        r = conn.execute("SELECT quantidade_total, status FROM produtos WHERE id=?",
                         (produto_id,)).fetchone()
        if not r:
            return resultado
        if r["status"] == "manutencao":
            for dia in range(1, ultimo_dia + 1):
                resultado.append({"dia": dia, "total": r["quantidade_total"],
                                  "disponivel": 0})
            return resultado
        total = r["quantidade_total"]
        for dia in range(1, ultimo_dia + 1):
            d = f"{ano}-{mes:02d}-{dia:02d}"
            reservado = conn.execute(
                "SELECT COALESCE(SUM(ip.quantidade), 0) FROM itens_pedido ip"
                " JOIN pedidos p ON p.id = ip.pedido_id"
                " WHERE ip.tipo = 'produto' AND ip.item_id = ?"
                " AND p.status_comercial != 'cancelado'"
                " AND p.data_retirada IS NOT NULL AND p.data_devolucao IS NOT NULL"
                " AND p.data_retirada <= ? AND p.data_devolucao >= ?",
                (produto_id, d, d)).fetchone()[0]
            resultado.append({"dia": dia, "total": total,
                              "disponivel": max(total - reservado, 0)})
    return resultado


# ---------------------------------------------------------------------------
# Fila unificada de pedidos + historico
# ---------------------------------------------------------------------------

def listar_pedidos_unificados() -> list:
    resultado = []
    with conectar() as conn:
        peds = conn.execute(
            "SELECT p.id, p.cliente_id, c.nome AS cliente_nome,"
            " p.data_evento, p.data_retirada, p.data_devolucao,"
            " p.status_comercial, p.status_operacional,"
            " p.observacoes, p.criado_em,"
            " COALESCE(SUM(i.quantidade * i.preco_unitario), 0) AS total,"
            " COUNT(i.id) AS itens_count"
            " FROM pedidos p"
            " LEFT JOIN clientes c ON c.id = p.cliente_id"
            " LEFT JOIN itens_pedido i ON i.pedido_id = p.id"
            " GROUP BY p.id"
        ).fetchall()
        for p in peds:
            p = dict(p)
            dt = p.get("data_evento") or ""
            antigo = dt != "" and dt < DATA_CORTE_FINALIZADOS
            sc = p["status_comercial"]
            so = p["status_operacional"]
            if antigo and sc not in ("cancelado", "finalizado"):
                sc = "finalizado"
            if antigo and so not in ("cancelado", "finalizado"):
                so = "finalizado"
            resultado.append({
                "tipo": "pedido",
                "id": p["id"],
                "numero": p["id"],
                "cliente_nome": p["cliente_nome"],
                "cliente_id": p["cliente_id"],
                "data_evento": p["data_evento"],
                "itens_count": p["itens_count"],
                "total": p["total"],
                "origem": "Morumbi Festas",
                "status_comercial": sc,
                "status_operacional": so,
                "data_retirada": p["data_retirada"],
                "data_devolucao": p["data_devolucao"],
                "criado_em": p["criado_em"],
                "observacoes": p.get("observacoes") or "",
            })

        hists = conn.execute(
            "SELECT h.id, h.cliente_id, c.nome AS cliente_nome,"
            " h.data_evento, h.descricao, h.valor, h.status_origem,"
            " h.origem, h.origem_id, h.canal, h.criado_em"
            " FROM eventos_historico h"
            " LEFT JOIN clientes c ON c.id = h.cliente_id"
        ).fetchall()
        for h in hists:
            h = dict(h)
            dt = h.get("data_evento") or ""
            antigo = dt != "" and dt < DATA_CORTE_FINALIZADOS
            sc = "finalizado" if antigo else (h.get("status_origem") or "—")
            so = "finalizado" if antigo else "—"
            resultado.append({
                "tipo": "historico",
                "id": h["id"],
                "numero": h.get("origem_id") or h["id"],
                "cliente_nome": h["cliente_nome"],
                "cliente_id": h["cliente_id"],
                "data_evento": h["data_evento"],
                "itens_count": 1,
                "total": h.get("valor") or 0,
                "origem": h.get("origem") or "Histórico importado",
                "status_comercial": sc,
                "status_operacional": so,
                "data_retirada": None,
                "data_devolucao": None,
                "criado_em": h["criado_em"],
                "observacoes": h.get("descricao") or "",
            })

    resultado.sort(
        key=lambda r: r.get("data_evento") or r.get("criado_em") or "",
        reverse=True,
    )
    return resultado


def origens_pedidos_unificados() -> list:
    with conectar() as conn:
        origens = ["Morumbi Festas"]
        rows = conn.execute(
            "SELECT DISTINCT origem FROM eventos_historico"
            " WHERE origem IS NOT NULL ORDER BY origem"
        ).fetchall()
        for r in rows:
            if r[0] and r[0] not in origens:
                origens.append(r[0])
        return origens


def indicadores_pedidos() -> dict:
    from datetime import timedelta
    hoje = date.today()
    daqui_30 = (hoje + timedelta(days=30)).isoformat()
    ano, mes = hoje.year, hoje.month
    primeiro_dia = f"{ano:04d}-{mes:02d}-01"
    if mes == 12:
        proximo_mes = f"{ano + 1:04d}-01-01"
    else:
        proximo_mes = f"{ano:04d}-{mes + 1:02d}-01"

    with conectar() as conn:
        abertos = conn.execute(
            "SELECT COUNT(*) FROM pedidos"
            " WHERE status_comercial IN ('confirmado', 'entregue')"
        ).fetchone()[0]

        proximos = conn.execute(
            "SELECT COUNT(*) FROM pedidos"
            " WHERE data_evento >= ? AND data_evento <= ?"
            " AND status_comercial IN ('confirmado', 'entregue')",
            (hoje.isoformat(), daqui_30),
        ).fetchone()[0]

        fin = conn.execute(
            "SELECT COALESCE(SUM(sub.total), 0), COUNT(*) FROM ("
            "  SELECT COALESCE(SUM(ip.quantidade * ip.preco_unitario), 0) AS total"
            "  FROM pedidos p"
            "  JOIN itens_pedido ip ON ip.pedido_id = p.id"
            "  WHERE p.status_comercial IN ('devolvido', 'finalizado')"
            "  AND COALESCE(p.data_devolucao, p.data_evento) >= ?"
            "  AND COALESCE(p.data_devolucao, p.data_evento) < ?"
            "  GROUP BY p.id"
            ") sub",
            (primeiro_dia, proximo_mes),
        ).fetchone()

        historico = conn.execute(
            "SELECT COUNT(*) FROM eventos_historico"
        ).fetchone()[0]

    return {
        "abertos": abertos,
        "proximos": proximos,
        "faturamento_total": fin[0],
        "faturamento_qtd": fin[1],
        "historico": historico,
    }


# ---------------------------------------------------------------------------
# Migracao: padronizar registros anteriores a DATA_CORTE_FINALIZADOS
# ---------------------------------------------------------------------------

def preview_migracao_finalizados() -> dict:
    with conectar() as conn:
        ped_afetados = conn.execute(
            "SELECT COUNT(*) FROM pedidos"
            " WHERE data_evento < ? AND data_evento != ''"
            " AND status_comercial NOT IN ('cancelado', 'finalizado')",
            (DATA_CORTE_FINALIZADOS,),
        ).fetchone()[0]

        hist_total = conn.execute(
            "SELECT COUNT(*) FROM eventos_historico"
            " WHERE data_evento < ? AND data_evento != ''",
            (DATA_CORTE_FINALIZADOS,),
        ).fetchone()[0]
        hist_3d = conn.execute(
            "SELECT COUNT(*) FROM eventos_historico"
            " WHERE data_evento < ? AND data_evento != ''"
            " AND origem = 'Morumbi 3D'",
            (DATA_CORTE_FINALIZADOS,),
        ).fetchone()[0]
        hist_outros = conn.execute(
            "SELECT COUNT(*) FROM eventos_historico"
            " WHERE data_evento < ? AND data_evento != ''"
            " AND (origem != 'Morumbi 3D' OR origem IS NULL)",
            (DATA_CORTE_FINALIZADOS,),
        ).fetchone()[0]

    return {
        "pedidos_afetados": ped_afetados,
        "historico_total": hist_total,
        "historico_3d_preservados": hist_3d,
        "historico_origem_morumbi_festas": hist_outros,
        "data_corte": DATA_CORTE_FINALIZADOS,
    }


def aplicar_migracao_finalizados() -> dict:
    preview = preview_migracao_finalizados()
    agora_ = formato.agora()
    with conectar() as conn:
        conn.execute(
            "UPDATE pedidos SET status_comercial = 'finalizado',"
            " status_operacional = 'finalizado', atualizado_em = ?"
            " WHERE data_evento < ? AND data_evento != ''"
            " AND status_comercial NOT IN ('cancelado', 'finalizado')",
            (agora_, DATA_CORTE_FINALIZADOS),
        )
        conn.execute(
            "UPDATE eventos_historico SET origem = 'Morumbi Festas'"
            " WHERE data_evento < ? AND data_evento != ''"
            " AND (origem != 'Morumbi 3D' OR origem IS NULL)",
            (DATA_CORTE_FINALIZADOS,),
        )
    registrar_acao(None, "migracao_finalizados",
                   f"Padronizou registros anteriores a {DATA_CORTE_FINALIZADOS}",
                   preview)
    return preview
