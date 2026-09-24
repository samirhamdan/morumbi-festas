"""Camada de dados — SQLite."""

import contextvars
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from sistema import formato

# Perfis por empresa (membros.perfil). Os valores gravados não mudam:
# "gestor" é o Gerente e "operacional" é a Operação na interface.
PERFIS = ("admin", "comercial", "operacional", "gestor", "financeiro",
          "visualizacao")
ROTULOS_PERFIL = {
    "admin": "Administrador", "gestor": "Gerente", "comercial": "Comercial",
    "operacional": "Operação", "financeiro": "Financeiro",
    "visualizacao": "Visualização",
}

ETAPAS_LEAD = (
    "novo", "atendimento", "orcamento", "negociacao",
    "reserva", "contratado", "concluido",
)
ETAPAS_ALT_LEAD = ("perdido", "cancelado")
STATUS_ORCAMENTO = ("rascunho", "enviado", "aceito", "recusado")
STATUS_PEDIDO_COMERCIAL = (
    "confirmado", "entregue", "devolvido", "finalizado", "cancelado",
)
STATUS_PEDIDO_OPERACIONAL = (
    "preparacao", "separado", "montado", "entregue",
    "recolhido", "conferido", "finalizado", "cancelado",
)
# Pedidos com estes status comerciais não geram trabalho, estoque nem alertas.
FORA_DA_OPERACAO = ("cancelado", "finalizado")
# Festa realizada e paga: entra no faturamento.
COMERCIAL_FATURADO = ("devolvido", "finalizado")
# Festa realizada: conta para a classificação do cliente.
COMERCIAL_REALIZADO = ("entregue", "devolvido", "finalizado")

DATA_CORTE_FINALIZADOS = "2026-09-23"

# Status vindos das origens externas (planilha, Morumbi 3D) em eventos_historico.
STATUS_ORIGEM_CANCELADO = ("cancelado", "cancelada")
STATUS_ORIGEM_REALIZADO = ("entregue",)
# Origens cujos dados vêm de outro sistema: valor e data não são editados aqui.
ORIGENS_SOMENTE_LEITURA = ("Morumbi 3D",)
# Origens mantidas no banco, mas fora de todas as telas e cálculos da Festas
# (o Morumbi 3D é outro negócio). Para voltar a exibir, esvazie a tupla.
ORIGENS_OCULTAS = ("Morumbi 3D",)

# Campos de texto livre do pedido (Sprint 2; canal no Sprint 2.1).
CAMPOS_TEXTO_PEDIDO = (
    "local_evento", "hora_retirada", "hora_devolucao", "responsavel",
    "forma_pagamento", "condicao_pagamento", "canal",
)

# Sprint 2.1 — operação, canal e fonte são conceitos separados.
# Operação: o negócio a que o pedido pertence. A planilha "Formulario Festas"
# é só a fonte técnica da importação; os pedidos dela são da Morumbi Festas.
# Nome usado quando a empresa ainda não tem nome cadastrado. A operação de
# cada pedido é a própria empresa (nome_empresa()).
OPERACAO_PRINCIPAL = "Morumbi Festas"
# Fontes que são outra operação (as demais pertencem à operação principal).
OPERACAO_PROPRIA_DA_FONTE = {"Morumbi 3D": "Morumbi 3D"}
ROTULOS_FONTE = {"Formulario Festas": "Formulário Festas"}
# Canal de aquisição: como o cliente chegou.
CANAIS = ("Google", "Instagram", "WhatsApp", "Facebook", "Formulário",
          "Indicação", "Site", "Outro")
_CANAL_POR_TRECHO = (
    ("google", "Google"), ("insta", "Instagram"), ("whats", "WhatsApp"),
    ("zap", "WhatsApp"), ("face", "Facebook"), ("indic", "Indicação"),
    ("amig", "Indicação"), ("formul", "Formulário"), ("site", "Site"),
)

CAMINHO_BD = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")


# ---------------------------------------------------------------------------
# Sprint 2.2 — empresa (tenant) atual
#
# Toda leitura e escrita dos dados de negócio acontece dentro de uma empresa.
# A empresa atual vem do contexto (definido pelo sistema a partir do membro
# autenticado, nunca de um valor enviado pela tela). Fora de uma requisição
# (ferramentas, testes) vale a empresa principal: a primeira cadastrada.
# ---------------------------------------------------------------------------

STATUS_EMPRESA = ("ativo", "suspenso", "inativo")
ROTULOS_STATUS_EMPRESA = {"ativo": "Ativa", "suspenso": "Suspensa",
                          "inativo": "Inativa"}
ORIGENS_LEAD_PADRAO = ("Instagram", "Facebook", "WhatsApp", "Google",
                       "Indicacao", "Site", "Recorrente")
# Tabelas com dados próprios de cada empresa. As tabelas filhas (itens,
# fotos, tags) pertencem à empresa do registro pai e só são alcançadas por ele.
TABELAS_DA_EMPRESA = (
    "clientes", "categorias", "produtos", "kits", "origens_lead", "leads",
    "orcamentos", "pedidos", "eventos_historico", "audit_log",
)
_tenant_ctx: contextvars.ContextVar = contextvars.ContextVar("tenant", default=None)
_tenant_padrao_cache: dict = {}


def tenant_padrao() -> int:
    """Empresa principal (a mais antiga): usada fora de requisições."""
    tid = _tenant_padrao_cache.get(CAMINHO_BD)
    if tid is None:
        with conectar() as conn:
            r = conn.execute("SELECT MIN(id) FROM organizacoes").fetchone()
        tid = r[0] or 1
        _tenant_padrao_cache[CAMINHO_BD] = tid
    return tid


def tenant_atual() -> int:
    tid = _tenant_ctx.get()
    return int(tid) if tid else tenant_padrao()


def definir_tenant(tenant_id):
    """Define a empresa do contexto atual; devolve o token para restaurar."""
    return _tenant_ctx.set(int(tenant_id) if tenant_id else None)


def restaurar_tenant(token):
    _tenant_ctx.reset(token)


@contextmanager
def usando_tenant(tenant_id):
    token = definir_tenant(tenant_id)
    try:
        yield
    finally:
        restaurar_tenant(token)


def _do_tenant(conn, tabela: str, id_) -> bool:
    """O registro existe e pertence à empresa atual?"""
    if not id_:
        return False
    return conn.execute(f"SELECT 1 FROM {tabela} WHERE id = ? AND {_t()}",
                        (id_,)).fetchone() is not None


def _validar_referencias(conn, dados_: dict):
    """Cliente, lead, origem e responsável citados precisam ser da empresa."""
    for campo, tabela, rotulo in (("cliente_id", "clientes", "Cliente"),
                                  ("lead_id", "leads", "Lead"),
                                  ("origem_id", "origens_lead", "Origem")):
        if dados_.get(campo) and not _do_tenant(conn, tabela, dados_[campo]):
            raise ErroDeCampo(campo, f"{rotulo} não encontrado.")
    if dados_.get("responsavel_id") and not conn.execute(
            f"SELECT 1 FROM membros WHERE usuario_id = ? AND {_t()}",
            (dados_["responsavel_id"],)).fetchone():
        raise ErroDeCampo("responsavel_id", "Responsável não encontrado.")


def _t(alias: str = "") -> str:
    """Filtro SQL da empresa atual (o id vem do contexto, nunca da tela)."""
    campo = f"{alias}.tenant_id" if alias else "tenant_id"
    return f"{campo} = {tenant_atual()}"


def situacao_historico(origem, status_origem, data_evento) -> str:
    """Situação de um registro de eventos_historico; a origem nunca é alterada."""
    status = (status_origem or "").strip().lower()
    if status in STATUS_ORIGEM_CANCELADO:
        return "cancelado"
    if status in STATUS_ORIGEM_REALIZADO:
        return "finalizado"
    if data_evento and data_evento < DATA_CORTE_FINALIZADOS:
        return "finalizado"
    return "pendente"


def _origem_visivel(alias: str = "h") -> str:
    """Importados da empresa atual, sem as origens ocultas."""
    if not ORIGENS_OCULTAS:
        return _t(alias)
    campo = f"{alias}.origem" if alias else "origem"
    lista = ",".join(f"'{o}'" for o in ORIGENS_OCULTAS)
    return f"COALESCE({campo}, '') NOT IN ({lista}) AND {_t(alias)}"


def _historico_visivel(alias: str = "h") -> str:
    """Importado visível que ainda não virou pedido atual (Sprint 2.1).

    O registro promovido continua em eventos_historico, intacto, mas passa a
    ser representado só pelo pedido: nada aparece nem é somado duas vezes.
    """
    campo = f"{alias}.id" if alias else "eventos_historico.id"
    return (f"{_origem_visivel(alias)} AND {campo} NOT IN"
            " (SELECT historico_id FROM pedidos WHERE historico_id IS NOT NULL)")


def _em_operacao(alias: str = "p") -> str:
    """Pedidos da empresa atual que ainda geram operação."""
    campo = f"{alias}.status_comercial" if alias else "status_comercial"
    return f"{campo} NOT IN ('cancelado', 'finalizado') AND {_t(alias)}"


def normalizar_texto(texto) -> str:
    """Minúsculas e sem acentos, para busca ('Thaís' encontra 'thais')."""
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def somente_digitos(texto) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def canal_canonico(texto) -> str:
    """Texto livre do canal ('pesquisa no google') -> canal padrão ('Google')."""
    t = normalizar_texto(texto).strip()
    if not t:
        return ""
    for trecho, canal in _CANAL_POR_TRECHO:
        if trecho in t:
            return canal
    return "Outro"


def operacao_da_fonte(fonte) -> str:
    return OPERACAO_PROPRIA_DA_FONTE.get(fonte or "", nome_empresa())


def rotulo_fonte(fonte) -> str:
    return ROTULOS_FONTE.get(fonte or "", fonte or "")


def _operacao_sql(campo: str) -> str:
    casos = " ".join(f"WHEN '{f}' THEN '{o}'"
                     for f, o in OPERACAO_PROPRIA_DA_FONTE.items())
    nome = nome_empresa().replace("'", "''")
    return f"CASE COALESCE({campo}, '') {casos} ELSE '{nome}' END"


def preparar_conexao(conn):
    conn.row_factory = sqlite3.Row
    conn.create_function("situacao_historico", 3, situacao_historico,
                         deterministic=True)
    conn.create_function("normalizar", 1, normalizar_texto, deterministic=True)
    conn.create_function("digitos", 1, somente_digitos, deterministic=True)
    conn.create_function("canal_canonico", 1, canal_canonico, deterministic=True)
    return conn


def conectar():
    conn = preparar_conexao(sqlite3.connect(CAMINHO_BD))
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
            for nome in ORIGENS_LEAD_PADRAO:
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
        if "historico" not in cols_pedido:
            conn.execute(
                "ALTER TABLE pedidos ADD COLUMN historico"
                " INTEGER NOT NULL DEFAULT 0")
        # Sprint 2: dados de logística e pagamento do pedido (opcionais)
        for coluna in CAMPOS_TEXTO_PEDIDO:
            if coluna not in cols_pedido:
                conn.execute(
                    f"ALTER TABLE pedidos ADD COLUMN {coluna} TEXT DEFAULT ''")

        # Sprint 2.1: vínculo do pedido atual com o registro importado de
        # origem (fonte técnica preservada) e valor informado na importação.
        for coluna, tipo in (("fonte", "TEXT DEFAULT ''"), ("fonte_id", "INTEGER"),
                             ("historico_id", "INTEGER"),
                             ("valor_informado", "REAL")):
            if coluna not in cols_pedido:
                conn.execute(f"ALTER TABLE pedidos ADD COLUMN {coluna} {tipo}")
        # Sprint 3.1: responsável escolhido entre os usuários da empresa. O
        # texto em "responsavel" continua guardado (nome na data da escolha e
        # nomes digitados antes da lista existir, que não são alterados).
        if "responsavel_id" not in cols_pedido:
            conn.execute("ALTER TABLE pedidos ADD COLUMN responsavel_id"
                         " INTEGER REFERENCES usuarios(id)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_pedidos_historico_id"
            " ON pedidos (historico_id) WHERE historico_id IS NOT NULL")

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

        _migrar_empresa(conn)
    # caches montados durante a migração podem estar incompletos
    _tenant_padrao_cache.pop(CAMINHO_BD, None)
    for cache in (_config_cache, _nome_cache):
        for chave in [k for k in cache if k[0] == CAMINHO_BD]:
            cache.pop(chave, None)


# ---------------------------------------------------------------------------
# Sprint 2.2 — migração para empresa (tenant). Só adiciona: nenhum registro é
# apagado, nenhum id muda, nenhum valor, data ou status é alterado.
# ---------------------------------------------------------------------------

_COLUNAS_EMPRESA = (
    ("nome_fantasia", "TEXT DEFAULT ''"), ("razao_social", "TEXT DEFAULT ''"),
    ("logo", "TEXT DEFAULT ''"), ("status", "TEXT NOT NULL DEFAULT 'ativo'"),
    # assinatura futura: vazios não afetam o funcionamento atual
    ("plano_id", "INTEGER"), ("status_assinatura", "TEXT"),
    ("atualizado_em", "TEXT"),
)

# Índices pensados nas consultas reais (listas, agenda, BI e busca).
_INDICES_EMPRESA = (
    ("ix_clientes_tenant", "clientes (tenant_id, status, nome)"),
    ("ix_categorias_tenant", "categorias (tenant_id)"),
    ("ix_produtos_tenant", "produtos (tenant_id, status)"),
    ("ix_kits_tenant", "kits (tenant_id, status)"),
    ("ix_origens_tenant", "origens_lead (tenant_id)"),
    ("ix_leads_tenant", "leads (tenant_id, status)"),
    ("ix_orcamentos_tenant", "orcamentos (tenant_id, status)"),
    ("ix_pedidos_tenant_status", "pedidos (tenant_id, status_comercial)"),
    ("ix_pedidos_tenant_evento", "pedidos (tenant_id, data_evento)"),
    ("ix_pedidos_tenant_retirada", "pedidos (tenant_id, data_retirada)"),
    ("ix_pedidos_tenant_devolucao", "pedidos (tenant_id, data_devolucao)"),
    ("ix_evt_hist_tenant_data", "eventos_historico (tenant_id, data_evento)"),
    ("ix_audit_tenant", "audit_log (tenant_id, tipo)"),
    ("ix_audit_entidade", "audit_log (tenant_id, entidade, entidade_id)"),
)


def _migrar_empresa(conn):
    agora_ = formato.agora()
    cols = _colunas(conn, "organizacoes")
    for coluna, tipo in _COLUNAS_EMPRESA:
        if coluna not in cols:
            conn.execute(f"ALTER TABLE organizacoes ADD COLUMN {coluna} {tipo}")

    tid = conn.execute("SELECT MIN(id) FROM organizacoes").fetchone()[0]
    if tid is None:
        tid = conn.execute(
            "INSERT INTO organizacoes (nome, cidade, status, criado_em, atualizado_em)"
            " VALUES ('Morumbi Festas', 'Campo Grande', 'ativo', ?, ?)",
            (agora_, agora_)).lastrowid

    cols = _colunas(conn, "usuarios")
    if "email" not in cols:
        conn.execute("ALTER TABLE usuarios ADD COLUMN email TEXT DEFAULT ''")
    if "plataforma_admin" not in cols:
        # futuro dono da plataforma; distinto do administrador da empresa
        conn.execute("ALTER TABLE usuarios ADD COLUMN plataforma_admin"
                     " INTEGER NOT NULL DEFAULT 0")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS membros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
            tenant_id INTEGER NOT NULL REFERENCES organizacoes(id),
            perfil TEXT NOT NULL DEFAULT 'comercial',
            ativo INTEGER NOT NULL DEFAULT 1,
            ultimo_acesso TEXT,
            criado_em TEXT NOT NULL DEFAULT (datetime('now')),
            atualizado_em TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (usuario_id, tenant_id)
        );
        CREATE INDEX IF NOT EXISTS ix_membros_tenant ON membros (tenant_id, ativo);

        CREATE TABLE IF NOT EXISTS configuracoes (
            tenant_id INTEGER NOT NULL REFERENCES organizacoes(id),
            chave TEXT NOT NULL,
            valor TEXT,
            tipo TEXT NOT NULL DEFAULT 'texto',
            atualizado_em TEXT,
            PRIMARY KEY (tenant_id, chave)
        );

        CREATE TABLE IF NOT EXISTS servicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES organizacoes(id),
            nome TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1,
            ordem INTEGER NOT NULL DEFAULT 0,
            criado_em TEXT NOT NULL DEFAULT (datetime('now')),
            atualizado_em TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (tenant_id, nome)
        );
    """)

    # Todos os dados existentes pertencem à empresa principal.
    for tabela in TABELAS_DA_EMPRESA:
        if "tenant_id" not in _colunas(conn, tabela):
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN tenant_id"
                         f" INTEGER NOT NULL DEFAULT {int(tid)}")
    _origens_unicas_por_empresa(conn, tid)

    cols = _colunas(conn, "audit_log")
    for coluna, tipo in (("entidade", "TEXT"), ("entidade_id", "INTEGER")):
        if coluna not in cols:
            conn.execute(f"ALTER TABLE audit_log ADD COLUMN {coluna} {tipo}")

    # Usuários sem vínculo (sistema anterior) viram membros da principal,
    # com o mesmo perfil e situação. Quem já tem vínculo não é tocado.
    conn.execute(
        "INSERT INTO membros (usuario_id, tenant_id, perfil, ativo, criado_em,"
        " atualizado_em) SELECT u.id, ?, u.perfil, u.ativo, u.criado_em, ?"
        " FROM usuarios u WHERE NOT EXISTS"
        " (SELECT 1 FROM membros m WHERE m.usuario_id = u.id)", (tid, agora_))

    # Parâmetros globais passam a ser configurações da principal.
    conn.execute(
        "INSERT OR IGNORE INTO configuracoes (tenant_id, chave, valor, atualizado_em)"
        " SELECT ?, chave, valor, ? FROM parametros", (tid, agora_))

    if not conn.execute("SELECT 1 FROM servicos WHERE tenant_id = ?",
                        (tid,)).fetchone():
        nomes: dict = {}
        for r in conn.execute("SELECT descricao FROM itens_pedido"
                              " WHERE tipo = 'servico' ORDER BY id"):
            nome = " ".join((r[0] or "").split())
            if nome:
                nomes.setdefault(normalizar_texto(nome), nome)
        for ordem, nome in enumerate(nomes.values() or SERVICOS_PADRAO):
            conn.execute("INSERT OR IGNORE INTO servicos (tenant_id, nome, ordem,"
                         " criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?)",
                         (tid, nome, ordem, agora_, agora_))

    # Chave de importação única dentro da empresa.
    conn.execute("DROP INDEX IF EXISTS ix_evt_hist_origem")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_evt_hist_tenant_origem"
                 " ON eventos_historico (tenant_id, origem, origem_id)")
    for nome, alvo in _INDICES_EMPRESA:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {nome} ON {alvo}")

    # Auditoria antiga de pedidos: preenche entidade a partir do próprio texto.
    for r in conn.execute("SELECT id, tipo, descricao FROM audit_log"
                          " WHERE entidade IS NULL AND tipo IN"
                          " ('pedido_evento', 'pedido', 'operacao')").fetchall():
        m = _RE_PEDIDO_LEGADO.search(r["descricao"] or "")
        if m:
            conn.execute("UPDATE audit_log SET entidade = 'pedido',"
                         " entidade_id = ? WHERE id = ?", (int(m.group(1)), r["id"]))


def _origens_unicas_por_empresa(conn, tid: int):
    """origens_lead.nome era único no banco todo; passa a ser por empresa.

    O SQLite não altera restrições: a tabela é recriada com os mesmos ids e
    registros (receita oficial: chaves estrangeiras desligadas durante a troca).
    """
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table'"
                       " AND name = 'origens_lead'").fetchone()[0]
    if "UNIQUE (tenant_id, nome)" in sql:
        return
    antes = conn.execute("SELECT COUNT(*) FROM origens_lead").fetchone()[0]
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(
            "CREATE TABLE origens_lead_nova ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " nome TEXT NOT NULL,"
            " criado_em TEXT NOT NULL DEFAULT (datetime('now')),"
            f" tenant_id INTEGER NOT NULL DEFAULT {int(tid)},"
            " UNIQUE (tenant_id, nome))")
        conn.execute("INSERT INTO origens_lead_nova (id, nome, criado_em, tenant_id)"
                     " SELECT id, nome, criado_em, tenant_id FROM origens_lead")
        depois = conn.execute("SELECT COUNT(*) FROM origens_lead_nova").fetchone()[0]
        if depois != antes:
            raise RuntimeError("Cópia de origens_lead incompleta.")
        conn.execute("DROP TABLE origens_lead")
        conn.execute("ALTER TABLE origens_lead_nova RENAME TO origens_lead")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Referências inválidas após recriar origens_lead.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_origens_tenant ON origens_lead (tenant_id)")


# ---------------------------------------------------------------------------
# Sprint 2.2 — empresa, configurações e membros
# ---------------------------------------------------------------------------

SERVICOS_PADRAO = ("Entrega", "Retirada", "Montagem", "Desmontagem",
                   "Recolhimento", "Devolução")

# Configurações por empresa (tabela configuracoes). Sem registro, vale o
# padrão abaixo. Status e transições críticas seguem no código.
CONFIG_PADRAO = {
    # Sistema
    "idioma": "pt-BR", "moeda": "BRL", "fuso_horario": "America/Campo_Grande",
    "formato_data": "DD/MM/YYYY",
    # Identidade visual (white label futuro)
    "cor_primaria": "#6F1C85", "cor_secundaria": "#FF8C00",
    # Empresa
    "horario_funcionamento": "", "facebook": "", "site": "",
    # Operação
    "dias_orcamento_sem_retorno": "3", "prazo_preparacao_dias": "",
    "prazo_devolucao_dias": "", "duracao_evento_padrao_horas": "",
    "regras_retirada": "", "regras_entrega": "", "regras_conferencia": "",
    # Financeiro (sem módulo financeiro ainda)
    "formas_pagamento": "Pix\nDinheiro\nCartão de crédito\nCartão de débito"
                        "\nTransferência\nBoleto",
    # Notificações futuras
    "notificacao_email": "0", "notificacao_whatsapp": "0",
    # Limites futuros (vazio = sem limite; nada é bloqueado ainda)
    "limite_usuarios": "", "limite_clientes": "", "limite_pedidos": "",
    "limite_produtos": "", "limite_armazenamento_mb": "",
}
ROTULOS_IDIOMA = {"pt-BR": "Português (Brasil)"}
ROTULOS_MOEDA = {"BRL": "Real (BRL)"}
FUSOS_HORARIOS = ("America/Campo_Grande", "America/Cuiaba", "America/Sao_Paulo",
                  "America/Manaus", "America/Porto_Velho", "America/Rio_Branco",
                  "America/Belem", "America/Fortaleza", "America/Recife",
                  "America/Noronha")
ROTULOS_FORMATO_DATA = {"DD/MM/YYYY": "DD/MM/AAAA"}
_config_cache: dict = {}
_fusos_cache: dict = {}


def config(chave: str, padrao: str | None = None) -> str:
    """Configuração da empresa atual (ou o padrão do sistema)."""
    tid = tenant_atual()
    cache = _config_cache.get((CAMINHO_BD, tid))
    if cache is None:
        with conectar() as conn:
            cache = {r["chave"]: r["valor"] for r in conn.execute(
                "SELECT chave, valor FROM configuracoes WHERE tenant_id = ?", (tid,))}
        _config_cache[(CAMINHO_BD, tid)] = cache
    if chave in cache and cache[chave] is not None:
        return cache[chave]
    return CONFIG_PADRAO.get(chave, "") if padrao is None else padrao


def salvar_config(valores: dict, usuario_id=None):
    tid = tenant_atual()
    agora_ = formato.agora()
    mudancas = {}
    with conectar() as conn:
        for chave, valor in valores.items():
            antes = config(chave)
            valor = "" if valor is None else str(valor)
            if antes == valor:
                continue
            conn.execute(
                "INSERT INTO configuracoes (tenant_id, chave, valor, atualizado_em)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (tenant_id, chave) DO UPDATE"
                " SET valor = excluded.valor, atualizado_em = excluded.atualizado_em",
                (tid, chave, valor, agora_))
            mudancas[chave] = [antes, valor]
        if mudancas:
            auditar(conn, "configuracao", tid, "alterar",
                    "Configurações alteradas: " + ", ".join(sorted(mudancas)),
                    usuario_id, mudancas)
    _config_cache.pop((CAMINHO_BD, tid), None)
    return mudancas


def lista_config(chave: str) -> list:
    return [v.strip() for v in config(chave).splitlines() if v.strip()]


def fuso_horario_atual():
    from zoneinfo import ZoneInfo
    nome = config("fuso_horario")
    if nome not in _fusos_cache:
        try:
            _fusos_cache[nome] = ZoneInfo(nome)
        except Exception:  # base de fusos ausente: mantém o horário de Campo Grande
            _fusos_cache[nome] = formato.FUSO
    return _fusos_cache[nome]


formato.definir_provedor_fuso(fuso_horario_atual)


def auditar(conn, entidade: str, entidade_id, acao: str, descricao: str,
            usuario_id=None, mudancas: dict | None = None, tipo: str | None = None,
            dados: dict | None = None):
    """Registro central de auditoria: empresa, usuário, entidade, registro,
    ação, data/hora e valores antes/depois (mudancas = {campo: [antes, depois]})."""
    corpo = dict(dados or {})
    corpo.setdefault("acao", acao)
    if mudancas:
        corpo["mudancas"] = mudancas
    conn.execute(
        "INSERT INTO audit_log (tenant_id, usuario_id, tipo, entidade, entidade_id,"
        " descricao, dados, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant_atual(), usuario_id, tipo or entidade, entidade, entidade_id,
         descricao, json.dumps(corpo, ensure_ascii=False), formato.agora()))


def empresa_atual() -> dict:
    with conectar() as conn:
        r = conn.execute("SELECT * FROM organizacoes WHERE id = ?",
                         (tenant_atual(),)).fetchone()
    e = dict(r) if r else {"id": tenant_atual(), "nome": "", "status": "ativo"}
    e["nome_exibicao"] = e.get("nome_fantasia") or e.get("nome") or ""
    return e


_nome_cache: dict = {}


def nome_empresa() -> str:
    """Nome de exibição da empresa atual (é também a operação dos pedidos)."""
    chave = (CAMINHO_BD, tenant_atual())
    if chave not in _nome_cache:
        _nome_cache[chave] = empresa_atual()["nome_exibicao"] or OPERACAO_PRINCIPAL
    return _nome_cache[chave]


def buscar_empresa(tenant_id: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute("SELECT * FROM organizacoes WHERE id = ?",
                         (tenant_id,)).fetchone()
    return dict(r) if r else None


def criar_empresa(nome: str, status: str = "ativo") -> int:
    """Nova empresa com as configurações padrão do sistema (base do onboarding
    futuro). Não copia nada de outra empresa."""
    nome = (nome or "").strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome da empresa é obrigatório.")
    if status not in STATUS_EMPRESA:
        raise ErroDeCampo("status", "Situação inválida.")
    agora_ = formato.agora()
    with conectar() as conn:
        tid = conn.execute(
            "INSERT INTO organizacoes (nome, cidade, status, criado_em, atualizado_em)"
            " VALUES (?, '', ?, ?, ?)", (nome, status, agora_, agora_)).lastrowid
        for ordem, s in enumerate(SERVICOS_PADRAO):
            conn.execute("INSERT INTO servicos (tenant_id, nome, ordem, criado_em,"
                         " atualizado_em) VALUES (?, ?, ?, ?, ?)",
                         (tid, s, ordem, agora_, agora_))
        for o in ORIGENS_LEAD_PADRAO:
            conn.execute("INSERT INTO origens_lead (nome, tenant_id) VALUES (?, ?)",
                         (o, tid))
    return tid


CAMPOS_EMPRESA = ("nome", "nome_fantasia", "razao_social", "cnpj", "telefone",
                  "whatsapp", "email", "endereco", "bairro", "cidade", "cep",
                  "instagram")


def salvar_empresa(valores: dict, usuario_id=None) -> dict:
    """Dados cadastrais da empresa atual; só os campos conhecidos."""
    atual = empresa_atual()
    novos = {c: (valores.get(c) or "").strip() for c in CAMPOS_EMPRESA if c in valores}
    if "nome" in novos and not novos["nome"]:
        raise ErroDeCampo("nome", "Nome da empresa é obrigatório.")
    for c, v in novos.items():
        if len(v) > 200:
            raise ErroDeCampo(c, "Texto muito longo (máximo 200 caracteres).")
    if novos.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", novos["email"]):
        raise ErroDeCampo("email", "E-mail inválido.")
    mudancas = {c: [atual.get(c) or "", v] for c, v in novos.items()
                if (atual.get(c) or "") != v}
    if mudancas:
        with conectar() as conn:
            conn.execute(
                "UPDATE organizacoes SET "
                + ", ".join(f"{c} = ?" for c in mudancas) + ", atualizado_em = ?"
                " WHERE id = ?",
                [novos[c] for c in mudancas] + [formato.agora(), tenant_atual()])
            auditar(conn, "empresa", tenant_atual(), "alterar",
                    "Dados da empresa alterados", usuario_id, mudancas)
        _nome_cache.pop((CAMINHO_BD, tenant_atual()), None)
    return mudancas


def salvar_logo_empresa(arquivo: str, usuario_id=None):
    antes = empresa_atual().get("logo") or ""
    with conectar() as conn:
        conn.execute("UPDATE organizacoes SET logo = ?, atualizado_em = ?"
                     " WHERE id = ?", (arquivo, formato.agora(), tenant_atual()))
        auditar(conn, "empresa", tenant_atual(), "alterar", "Logo da empresa alterado",
                usuario_id, {"logo": [antes, arquivo]})
    return antes


# --- Serviços da empresa ----------------------------------------------------

def listar_servicos(somente_ativos: bool = False) -> list:
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM servicos WHERE {_t()}"
            + (" AND ativo = 1" if somente_ativos else "")
            + " ORDER BY ordem, nome")]


def salvar_servico(nome: str, usuario_id=None) -> int:
    nome = " ".join((nome or "").split())
    if not nome:
        raise ErroDeCampo("nome", "Nome do serviço é obrigatório.")
    if len(nome) > 80:
        raise ErroDeCampo("nome", "Nome muito longo (máximo 80 caracteres).")
    with conectar() as conn:
        for r in conn.execute(f"SELECT id, nome FROM servicos WHERE {_t()}"):
            if normalizar_texto(r["nome"]) == normalizar_texto(nome):
                raise ErroDeCampo("nome", f"O serviço {r['nome']} já existe.")
        ordem = conn.execute(f"SELECT COALESCE(MAX(ordem), -1) + 1 FROM servicos"
                             f" WHERE {_t()}").fetchone()[0]
        agora_ = formato.agora()
        sid = conn.execute(
            "INSERT INTO servicos (tenant_id, nome, ordem, criado_em, atualizado_em)"
            " VALUES (?, ?, ?, ?, ?)", (tenant_atual(), nome, ordem, agora_, agora_)
        ).lastrowid
        auditar(conn, "servico", sid, "criar", f"Serviço {nome} criado", usuario_id)
    return sid


def alternar_servico(servico_id: int, usuario_id=None) -> bool:
    with conectar() as conn:
        r = conn.execute(f"SELECT * FROM servicos WHERE id = ? AND {_t()}",
                         (servico_id,)).fetchone()
        if not r:
            raise ValueError("Serviço não encontrado.")
        novo = 0 if r["ativo"] else 1
        conn.execute("UPDATE servicos SET ativo = ?, atualizado_em = ?"
                     f" WHERE id = ? AND {_t()}", (novo, formato.agora(), servico_id))
        auditar(conn, "servico", servico_id, "ativar" if novo else "desativar",
                f"Serviço {r['nome']} {'ativado' if novo else 'desativado'}",
                usuario_id, {"ativo": [r["ativo"], novo]})
    return bool(novo)


def mover_servico(servico_id: int, direcao: int, usuario_id=None):
    servicos = listar_servicos()
    ids = [s["id"] for s in servicos]
    if servico_id not in ids:
        raise ValueError("Serviço não encontrado.")
    i = ids.index(servico_id)
    j = i + (1 if direcao > 0 else -1)
    if not 0 <= j < len(ids):
        return
    ids[i], ids[j] = ids[j], ids[i]
    with conectar() as conn:
        for ordem, sid in enumerate(ids):
            conn.execute(f"UPDATE servicos SET ordem = ? WHERE id = ? AND {_t()}",
                         (ordem, sid))


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
            "INSERT INTO audit_log (tenant_id, usuario_id, tipo, descricao, dados,"
            " criado_em) VALUES (?, ?, ?, ?, ?, ?)",
            (tenant_atual(), usuario_id, tipo, descricao, d, formato.agora()))


def listar_audit(limite=100) -> list:
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, u.nome AS usuario_nome FROM audit_log a"
            " LEFT JOIN usuarios u ON u.id = a.usuario_id"
            f" WHERE {_t('a')} ORDER BY a.id DESC LIMIT ?", (limite,)).fetchall()]


# ---------------------------------------------------------------------------
# Usuarios e membros (Sprint 2.2)
#
# usuarios = identidade (nome, login, senha), única na plataforma.
# membros  = vínculo do usuário com uma empresa: perfil, situação e último
#            acesso naquela empresa. Um usuário pode, no futuro, ter vários.
# ---------------------------------------------------------------------------

_SQL_MEMBRO = (
    "SELECT u.id, u.nome, u.login, u.email, u.senha_hash, u.criado_em,"
    " u.plataforma_admin, u.ativo AS usuario_ativo, m.id AS membro_id,"
    " m.tenant_id, m.perfil, m.ativo AS membro_ativo, m.ultimo_acesso,"
    " CASE WHEN u.ativo = 1 AND m.ativo = 1 THEN 1 ELSE 0 END AS ativo"
    " FROM membros m JOIN usuarios u ON u.id = m.usuario_id")


def existe_usuario() -> bool:
    with conectar() as conn:
        return conn.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is not None


def listar_usuarios(somente_ativos=True) -> list:
    """Membros da empresa atual."""
    sql = f"{_SQL_MEMBRO} WHERE {_t('m')}"
    if somente_ativos:
        sql += " AND u.ativo = 1 AND m.ativo = 1"
    sql += " ORDER BY u.nome"
    with conectar() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def buscar_usuario(id_: int) -> dict | None:
    """Usuário como membro da empresa atual (None se não pertence a ela)."""
    with conectar() as conn:
        r = conn.execute(f"{_SQL_MEMBRO} WHERE u.id = ? AND {_t('m')}",
                         (id_,)).fetchone()
        return dict(r) if r else None


def buscar_usuario_por_login(login: str) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT * FROM usuarios WHERE login = ?", (login,)).fetchone()
        return dict(r) if r else None


def membros_do_usuario(usuario_id: int) -> list:
    """Empresas em que o usuário pode entrar (vínculo ativo, empresa ativa)."""
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            f"{_SQL_MEMBRO} JOIN organizacoes o ON o.id = m.tenant_id"
            " WHERE u.id = ? AND u.ativo = 1 AND m.ativo = 1 AND o.status = 'ativo'"
            " ORDER BY m.tenant_id", (usuario_id,)).fetchall()]


def membro(usuario_id: int, tenant_id: int) -> dict | None:
    """Vínculo válido para uso: usuário e vínculo ativos e empresa ativa."""
    with conectar() as conn:
        r = conn.execute(
            f"{_SQL_MEMBRO} JOIN organizacoes o ON o.id = m.tenant_id"
            " WHERE u.id = ? AND m.tenant_id = ? AND u.ativo = 1 AND m.ativo = 1"
            " AND o.status = 'ativo'", (usuario_id, tenant_id)).fetchone()
        return dict(r) if r else None


def registrar_acesso(usuario_id: int, tenant_id: int):
    with conectar() as conn:
        conn.execute("UPDATE membros SET ultimo_acesso = ? WHERE usuario_id = ?"
                     " AND tenant_id = ?", (formato.agora(), usuario_id, tenant_id))


def adicionar_membro(usuario_id: int, tenant_id: int, perfil: str) -> int:
    if perfil not in PERFIS:
        raise ErroDeCampo("perfil", "Perfil inválido.")
    agora_ = formato.agora()
    with conectar() as conn:
        return conn.execute(
            "INSERT INTO membros (usuario_id, tenant_id, perfil, ativo, criado_em,"
            " atualizado_em) VALUES (?, ?, ?, 1, ?, ?)",
            (usuario_id, tenant_id, perfil, agora_, agora_)).lastrowid


def campos_usuario(form) -> dict:
    return {
        "nome": (form.get("nome") or "").strip(),
        "login": (form.get("login") or "").strip().lower(),
        "email": (form.get("email") or "").strip().lower(),
        "perfil": form.get("perfil", "comercial"),
        "ativo": int(form.get("ativo", 1)),
    }


class ErroDeCampo(ValueError):
    def __init__(self, campo: str, mensagem: str):
        self.campo = campo
        super().__init__(mensagem)


def _admins_ativos(conn, exceto: int | None = None) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM membros m JOIN usuarios u ON u.id = m.usuario_id"
        f" WHERE {_t('m')} AND m.perfil = 'admin' AND m.ativo = 1 AND u.ativo = 1"
        " AND u.id != ?", (exceto or 0,)).fetchone()[0]


def salvar_usuario(dados: dict, senha_hash: str | None = None,
                   id_: int | None = None, usuario_id=None) -> int:
    """Cria ou edita um membro da empresa atual.

    Identidade (nome, login, e-mail, senha) é do usuário; perfil e situação
    são do vínculo com esta empresa. Só edita quem é membro dela.
    """
    nome = dados.get("nome", "").strip()
    login = dados.get("login", "").strip().lower()
    email = (dados.get("email") or "").strip().lower()
    perfil = dados.get("perfil", "comercial")
    ativo = int(dados.get("ativo", 1))

    if not nome:
        raise ErroDeCampo("nome", "Nome é obrigatório.")
    if not login:
        raise ErroDeCampo("login", "Login é obrigatório.")
    if perfil not in PERFIS:
        raise ErroDeCampo("perfil", "Perfil inválido.")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ErroDeCampo("email", "E-mail inválido.")

    tid = tenant_atual()
    agora = formato.agora()
    with conectar() as conn:
        existente = conn.execute(
            "SELECT id FROM usuarios WHERE login = ? AND id != ?",
            (login, id_ or 0)).fetchone()
        if existente:
            raise ErroDeCampo("login", "Já existe um usuário com esse login.")

        if id_:
            atual = conn.execute(f"{_SQL_MEMBRO} WHERE u.id = ? AND {_t('m')}",
                                 (id_,)).fetchone()
            if not atual:
                raise ValueError("Usuário não encontrado.")
            deixa_admin = atual["perfil"] == "admin" and (perfil != "admin" or not ativo)
            if deixa_admin and atual["ativo"] and not _admins_ativos(conn, exceto=id_):
                raise ErroDeCampo("perfil", "A empresa precisa de pelo menos um"
                                            " administrador ativo.")
            campos = "nome=?, login=?, email=?, perfil=?, atualizado_em=?"
            params = [nome, login, email, perfil, agora]
            if senha_hash:
                campos += ", senha_hash=?"
                params.append(senha_hash)
            conn.execute(f"UPDATE usuarios SET {campos} WHERE id = ?", params + [id_])
            conn.execute("UPDATE membros SET perfil = ?, ativo = ?, atualizado_em = ?"
                         " WHERE usuario_id = ? AND tenant_id = ?",
                         (perfil, ativo, agora, id_, tid))
            mudancas = {c: [atual[c] or "", v] for c, v in (
                ("nome", nome), ("login", login), ("email", email),
                ("perfil", perfil), ("ativo", ativo)) if str(atual[c] or "") != str(v)}
            if senha_hash:
                mudancas["senha"] = ["", "alterada"]
            if mudancas:
                auditar(conn, "usuario", id_, "alterar", f"Usuário {nome} alterado",
                        usuario_id, mudancas)
            return id_
        if not senha_hash:
            raise ErroDeCampo("senha", "Senha é obrigatória para novo usuário.")
        novo = conn.execute(
            "INSERT INTO usuarios (nome, login, email, senha_hash, perfil, ativo,"
            " criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (nome, login, email, senha_hash, perfil, agora, agora)).lastrowid
        conn.execute(
            "INSERT INTO membros (usuario_id, tenant_id, perfil, ativo, criado_em,"
            " atualizado_em) VALUES (?, ?, ?, ?, ?, ?)",
            (novo, tid, perfil, ativo, agora, agora))
        auditar(conn, "usuario", novo, "criar", f"Usuário {nome} criado", usuario_id,
                {"perfil": ["", perfil]})
        return novo


def alternar_usuario(id_: int, usuario_id=None) -> bool:
    """Ativa/desativa o vínculo do usuário com a empresa atual."""
    u = buscar_usuario(id_)
    if not u:
        raise ValueError("Usuário não encontrado.")
    if id_ == usuario_id:
        raise ValueError("Você não pode desativar o próprio acesso.")
    novo = 0 if u["membro_ativo"] else 1
    with conectar() as conn:
        if not novo and u["perfil"] == "admin" and not _admins_ativos(conn, exceto=id_):
            raise ValueError("A empresa precisa de pelo menos um administrador ativo.")
        conn.execute("UPDATE membros SET ativo = ?, atualizado_em = ?"
                     " WHERE usuario_id = ? AND tenant_id = ?",
                     (novo, formato.agora(), id_, tenant_atual()))
        auditar(conn, "usuario", id_, "ativar" if novo else "desativar",
                f"Usuário {u['nome']} {'ativado' if novo else 'desativado'}",
                usuario_id, {"ativo": [u["membro_ativo"], novo]})
    return bool(novo)


# ---------------------------------------------------------------------------
# Parametros
# ---------------------------------------------------------------------------

def parametro(chave: str, padrao: str = "") -> str:
    """Compatibilidade: parâmetros agora são configurações da empresa."""
    valor = config(chave, "")
    return valor if valor != "" else (CONFIG_PADRAO.get(chave) or padrao)


def salvar_parametro(chave: str, valor: str):
    salvar_config({chave: valor})


# ---------------------------------------------------------------------------
# Organizacao
# ---------------------------------------------------------------------------

def organizacao() -> dict:
    """Compatibilidade: a organização é a empresa atual."""
    return empresa_atual()


def salvar_organizacao(dados: dict) -> int:
    salvar_empresa({c: v for c, v in dados.items() if c in CAMPOS_EMPRESA})
    return tenant_atual()


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------

ORIGENS_CLIENTE = (
    "Instagram", "WhatsApp", "Indicacao", "Google", "Facebook",
    "Evento", "Loja fisica", "Outro",
)


def listar_clientes(somente_ativos=True) -> list:
    sql = f"SELECT * FROM clientes WHERE {_t()}"
    if somente_ativos:
        sql += " AND status = 'ativo'"
    sql += " ORDER BY nome"
    with conectar() as conn:
        clientes = [dict(r) for r in conn.execute(sql).fetchall()]
        for c in clientes:
            c["tags"] = _tags_do_cliente(conn, c["id"])
        return clientes


def buscar_cliente(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(f"SELECT * FROM clientes WHERE id = ? AND {_t()}",
                         (id_,)).fetchone()
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
        sql = (f"SELECT id, nome, {campo} FROM clientes WHERE {campo} = ?"
               f" AND id != ? AND {_t()}")
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
            if not conn.execute(f"SELECT 1 FROM clientes WHERE id = ? AND {_t()}",
                                (id_,)).fetchone():
                raise ValueError("Cliente não encontrado.")
            conn.execute(
                "UPDATE clientes SET nome=?, cpf_cnpj=?, whatsapp=?, telefone=?,"
                " email=?, data_nascimento=?, endereco=?, bairro=?, cidade=?,"
                " cep=?, instagram=?, observacoes=?, origem=?, status=?,"
                f" cliente_3d_id=?, atualizado_em=? WHERE id = ? AND {_t()}",
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
                "INSERT INTO clientes (tenant_id, nome, cpf_cnpj, whatsapp, telefone,"
                " email, data_nascimento, endereco, bairro, cidade, cep,"
                " instagram, observacoes, origem, status, cliente_3d_id,"
                " criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_atual(), nome, cpf, whatsapp, dados_.get("telefone", ""),
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


def tags_em_uso(tabela: str) -> list:
    """Tags usadas pelos clientes ou produtos da empresa atual."""
    filha, chave = {"clientes": ("tags_cliente", "cliente_id"),
                    "produtos": ("tags_produto", "produto_id")}[tabela]
    with conectar() as conn:
        return [r["tag"] for r in conn.execute(
            f"SELECT DISTINCT t.tag FROM {filha} t JOIN {tabela} x ON x.id = t.{chave}"
            f" WHERE {_t('x')} ORDER BY t.tag")]


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
            f"SELECT * FROM categorias WHERE {_t()} ORDER BY ordem, nome").fetchall()]


def buscar_categoria(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(f"SELECT * FROM categorias WHERE id = ? AND {_t()}",
                         (id_,)).fetchone()
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
        if pai_id and not conn.execute(
                f"SELECT 1 FROM categorias WHERE id = ? AND {_t()}", (pai_id,)).fetchone():
            raise ErroDeCampo("pai_id", "Categoria principal não encontrada.")
        if id_:
            if not conn.execute(f"SELECT 1 FROM categorias WHERE id = ? AND {_t()}",
                                (id_,)).fetchone():
                raise ErroDeCampo("nome", "Categoria não encontrada.")
            conn.execute(f"UPDATE categorias SET nome=?, pai_id=? WHERE id=? AND {_t()}",
                         (nome, pai_id, id_))
            return id_
        r = conn.execute(
            "INSERT INTO categorias (tenant_id, nome, pai_id, criado_em)"
            " VALUES (?, ?, ?, ?)", (tenant_atual(), nome, pai_id, formato.agora()))
        return r.lastrowid


def excluir_categoria(id_: int):
    with conectar() as conn:
        if not conn.execute(f"SELECT 1 FROM categorias WHERE id = ? AND {_t()}",
                            (id_,)).fetchone():
            raise ErroDeCampo("nome", "Categoria não encontrada.")
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
        conn.execute(f"DELETE FROM categorias WHERE id = ? AND {_t()}", (id_,))


def _prefixo_categoria(conn, categoria_id: int | None) -> str:
    if not categoria_id:
        return "GER"
    r = conn.execute(f"SELECT nome FROM categorias WHERE id = ? AND {_t()}",
                     (categoria_id,)).fetchone()
    if not r:
        return "GER"
    nome = r["nome"].upper().replace(" ", "")
    return nome[:3] if len(nome) >= 3 else nome.ljust(3, "X")


def gerar_sku(categoria_id: int | None = None) -> str:
    with conectar() as conn:
        prefixo = _prefixo_categoria(conn, categoria_id)
        # o código é único na plataforma (restrição do banco): a sequência
        # olha todas as empresas e pula códigos já usados
        r = conn.execute(
            "SELECT COUNT(*) FROM produtos WHERE codigo_sku LIKE ?",
            (f"{prefixo}-%",)).fetchone()[0]
        seq = r + 1
        while conn.execute("SELECT 1 FROM produtos WHERE codigo_sku = ?",
                           (f"{prefixo}-{seq:04d}",)).fetchone():
            seq += 1
        return f"{prefixo}-{seq:04d}"


# ---------------------------------------------------------------------------
# Produtos
# ---------------------------------------------------------------------------

STATUS_PRODUTO = ("disponivel", "manutencao", "inativo")


def listar_produtos(status: str | None = None) -> list:
    sql = ("SELECT p.*, c.nome AS categoria_nome"
           " FROM produtos p LEFT JOIN categorias c ON c.id = p.categoria_id"
           f" WHERE {_t('p')}")
    params = []
    if status:
        sql += " AND p.status = ?"
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
            f" WHERE p.id = ? AND {_t('p')}", (id_,)).fetchone()
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
        if dados_.get("categoria_id") and not _do_tenant(
                conn, "categorias", dados_["categoria_id"]):
            raise ErroDeCampo("categoria_id", "Categoria não encontrada.")
        if id_:
            prod = conn.execute(f"SELECT codigo_sku FROM produtos WHERE id=? AND {_t()}",
                                (id_,)).fetchone()
            if not prod:
                raise ValueError("Produto não encontrado.")
            conn.execute(
                "UPDATE produtos SET nome=?, categoria_id=?, descricao=?,"
                " preco_locacao=?, valor_referencia=?, quantidade_total=?,"
                " status=?, publicado=?, localizacao=?, observacoes=?,"
                f" atualizado_em=? WHERE id = ? AND {_t()}",
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
                "INSERT INTO produtos (tenant_id, codigo_sku, nome, categoria_id,"
                " descricao, preco_locacao, valor_referencia, quantidade_total,"
                " status, publicado, localizacao, observacoes, criado_em,"
                " atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_atual(), sku, nome, dados_.get("categoria_id"),
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
        r = conn.execute("SELECT quantidade_total, status FROM produtos"
                         f" WHERE id=? AND {_t()}", (produto_id,)).fetchone()
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
            f" AND {_em_operacao()} AND {_t('p')}"
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
        if not _do_tenant(conn, "produtos", produto_id):
            raise ValueError("Produto não encontrado.")
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
        if not _do_tenant(conn, "produtos", produto_id) or not conn.execute(
                "SELECT 1 FROM fotos_produto WHERE id=? AND produto_id=?",
                (foto_id, produto_id)).fetchone():
            return
        conn.execute("UPDATE fotos_produto SET principal=0 WHERE produto_id=?",
                     (produto_id,))
        conn.execute("UPDATE fotos_produto SET principal=1 WHERE id=?", (foto_id,))


def excluir_foto_produto(foto_id: int) -> dict | None:
    with conectar() as conn:
        foto = conn.execute(
            "SELECT f.* FROM fotos_produto f JOIN produtos p ON p.id = f.produto_id"
            f" WHERE f.id=? AND {_t('p')}", (foto_id,)).fetchone()
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
    sql = f"SELECT * FROM kits WHERE {_t()}"
    params = []
    if status:
        sql += " AND status = ?"
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
        r = conn.execute(f"SELECT * FROM kits WHERE id = ? AND {_t()}",
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
            if not _do_tenant(conn, "kits", id_):
                raise ValueError("Kit não encontrado.")
            conn.execute(
                "UPDATE kits SET nome=?, descricao=?, preco=?,"
                f" status=?, publicado=?, atualizado_em=? WHERE id = ? AND {_t()}",
                (nome, dados_.get("descricao", ""),
                 dados_.get("preco", 0), status,
                 dados_.get("publicado", 0), agora_, id_))
            return id_
        r = conn.execute(
            "INSERT INTO kits (tenant_id, nome, descricao, preco, status,"
            " publicado, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?)",
            (tenant_atual(), nome, dados_.get("descricao", ""),
             dados_.get("preco", 0), status,
             dados_.get("publicado", 0), agora_, agora_))
        return r.lastrowid


def adicionar_item_kit(kit_id: int, produto_id: int, quantidade: int = 1) -> int:
    if quantidade < 1:
        raise ErroDeCampo("quantidade", "Quantidade mínima é 1.")
    with conectar() as conn:
        if not _do_tenant(conn, "kits", kit_id):
            raise ErroDeCampo("kit_id", "Kit não encontrado.")
        if not _do_tenant(conn, "produtos", produto_id):
            raise ErroDeCampo("produto_id", "Produto não encontrado.")
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


def remover_item_kit(item_id: int, kit_id: int | None = None):
    with conectar() as conn:
        conn.execute(
            "DELETE FROM itens_kit WHERE id = ? AND kit_id IN"
            f" (SELECT id FROM kits WHERE {_t()})"
            + (" AND kit_id = ?" if kit_id else ""),
            (item_id, kit_id) if kit_id else (item_id,))


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
        if not _do_tenant(conn, "kits", kit_id):
            raise ValueError("Kit não encontrado.")
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
        if not _do_tenant(conn, "kits", kit_id) or not conn.execute(
                "SELECT 1 FROM fotos_kit WHERE id=? AND kit_id=?",
                (foto_id, kit_id)).fetchone():
            return
        conn.execute("UPDATE fotos_kit SET principal=0 WHERE kit_id=?",
                     (kit_id,))
        conn.execute("UPDATE fotos_kit SET principal=1 WHERE id=?", (foto_id,))


def excluir_foto_kit(foto_id: int) -> dict | None:
    with conectar() as conn:
        foto = conn.execute(
            "SELECT f.* FROM fotos_kit f JOIN kits k ON k.id = f.kit_id"
            f" WHERE f.id=? AND {_t('k')}", (foto_id,)).fetchone()
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
    dia = date.fromisoformat(_hoje_iso())
    hoje = dia.isoformat()
    amanha = (dia + timedelta(days=1)).isoformat()
    proximos_7 = (dia + timedelta(days=7)).isoformat()

    with conectar() as conn:
        total_clientes = conn.execute(
            f"SELECT COUNT(*) FROM clientes WHERE status='ativo' AND {_t()}"
        ).fetchone()[0]
        total_produtos = conn.execute(
            f"SELECT COUNT(*) FROM produtos WHERE status != 'inativo' AND {_t()}"
        ).fetchone()[0]
        total_kits = conn.execute(
            f"SELECT COUNT(*) FROM kits WHERE status = 'ativo' AND {_t()}").fetchone()[0]
        total_leads = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE status NOT IN"
            f" ('perdido','cancelado','concluido') AND {_t()}").fetchone()[0]
        total_orcamentos = conn.execute(
            "SELECT COUNT(*) FROM orcamentos WHERE status IN ('rascunho','enviado')"
            f" AND {_t()}").fetchone()[0]
        total_pedidos = conn.execute(
            f"SELECT COUNT(*) FROM pedidos WHERE {_em_operacao('')}"
        ).fetchone()[0]
        pendencias_op = conn.execute(
            "SELECT COUNT(*) FROM pedidos"
            f" WHERE {_em_operacao('')}"
            " AND status_operacional IN ('preparacao','separado','montado')"
        ).fetchone()[0]

        retiradas_hoje = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_retirada, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            f" WHERE p.data_retirada=? AND {_em_operacao()}"
            " ORDER BY c.nome", (hoje,)).fetchall()]

        devolucoes_hoje = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_devolucao, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            f" WHERE p.data_devolucao=? AND {_em_operacao()}"
            " ORDER BY c.nome", (hoje,)).fetchall()]

        devolucoes_atrasadas = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_devolucao, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            f" WHERE p.data_devolucao < ? AND {_em_operacao()}"
            " AND p.status_operacional NOT IN ('recolhido','conferido','finalizado','cancelado')"
            " ORDER BY p.data_devolucao", (hoje,)).fetchall()]

        orcamentos_sem_retorno = [dict(r) for r in conn.execute(
            "SELECT o.id, o.criado_em, c.nome AS cliente_nome"
            " FROM orcamentos o LEFT JOIN clientes c ON c.id=o.cliente_id"
            f" WHERE o.status='enviado' AND {_t('o')}"
            " ORDER BY o.criado_em", ).fetchall()]

        proximos_eventos = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_evento, p.data_retirada, p.data_devolucao,"
            " p.status_comercial, p.status_operacional,"
            " c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id=p.cliente_id"
            f" WHERE {_em_operacao()}"
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


def _data_pedido_sql(alias: str = "p") -> str:
    """Data que define o período do pedido: evento; sem ela, devolução ou retirada."""
    a = alias
    return (f"COALESCE(NULLIF({a}.data_evento, ''), NULLIF({a}.data_devolucao, ''),"
            f" NULLIF({a}.data_retirada, ''))")


def _total_pedido_sql(alias: str = "p") -> str:
    """Soma dos itens; sem itens, o valor informado na importação (ou NULL)."""
    return (f"COALESCE((SELECT SUM(i.quantidade * i.preco_unitario)"
            f" FROM itens_pedido i WHERE i.pedido_id = {alias}.id),"
            f" NULLIF({alias}.valor_informado, 0))")


def total_do_pedido(ped: dict) -> float:
    if ped.get("itens"):
        return sum(i["quantidade"] * i["preco_unitario"] for i in ped["itens"])
    return ped.get("valor_informado") or 0


def faturamento_periodo(inicio: str, fim: str) -> dict:
    """Soma dos pedidos finalizados (atuais e históricos) com data em [inicio, fim].

    Nunca usa criado_em: em registros importados ele é a data da importação.
    """
    faturados = ",".join(f"'{s}'" for s in COMERCIAL_FATURADO)
    with conectar() as conn:
        peds = conn.execute(
            "SELECT * FROM ("
            " SELECT p.id, p.cliente_id, c.nome AS cliente_nome, p.historico,"
            f"  {_data_pedido_sql()} AS data, p.fonte, p.fonte_id,"
            f"  {_total_pedido_sql()} AS valor"
            " FROM pedidos p LEFT JOIN clientes c ON c.id = p.cliente_id"
            f" WHERE p.status_comercial IN ({faturados}) AND {_t('p')}"
            ") WHERE data BETWEEN ? AND ? ORDER BY data, id",
            (inicio, fim)).fetchall()
        hists = conn.execute(
            "SELECT h.id, h.origem_id, h.origem, h.cliente_id,"
            " c.nome AS cliente_nome, h.data_evento AS data, h.valor"
            " FROM eventos_historico h LEFT JOIN clientes c ON c.id = h.cliente_id"
            " WHERE situacao_historico(h.origem, h.status_origem, h.data_evento) = 'finalizado'"
            f" AND {_historico_visivel()}"
            " AND h.data_evento BETWEEN ? AND ? ORDER BY h.data_evento, h.id",
            (inicio, fim)).fetchall()

    # origem = operação (a empresa); fonte = de onde o registro veio.
    operacao = nome_empresa()
    registros = [{"tipo": "pedido", "id": r["id"], "numero": r["id"],
                  "origem": operacao, "fonte": r["fonte"] or "",
                  "numero_origem": r["fonte_id"],
                  "historico": bool(r["historico"]),
                  "cliente_id": r["cliente_id"], "cliente_nome": r["cliente_nome"],
                  "data": r["data"], "valor": r["valor"] or None} for r in peds]
    registros += [{"tipo": "historico", "id": r["id"],
                   "numero": r["origem_id"] or r["id"],
                   "origem": operacao_da_fonte(r["origem"]), "fonte": r["origem"] or "",
                   "numero_origem": r["origem_id"], "historico": True,
                   "cliente_id": r["cliente_id"], "cliente_nome": r["cliente_nome"],
                   "data": r["data"], "valor": r["valor"] or None} for r in hists]
    registros.sort(key=lambda r: (r["data"], r["tipo"], r["id"]))

    por_fonte: dict = {}
    for r in registros:
        rotulo = ("Registrados no sistema" if r["tipo"] == "pedido"
                  else f"Importados · {rotulo_fonte(r['fonte'])}")
        o = por_fonte.setdefault(rotulo, {"total": 0.0, "quantidade": 0,
                                          "sem_valor": 0})
        o["quantidade"] += 1
        if r["valor"] is None:
            o["sem_valor"] += 1
        else:
            o["total"] += r["valor"]
    return {
        "inicio": inicio, "fim": fim,
        "total": sum(o["total"] for o in por_fonte.values()),
        "quantidade": len(registros),
        "sem_valor": sum(o["sem_valor"] for o in por_fonte.values()),
        "por_fonte": por_fonte,
        "registros": registros,
    }


def _intervalo_mes(ano: int, mes: int) -> tuple[str, str]:
    import calendar
    return (f"{ano}-{mes:02d}-01",
            f"{ano}-{mes:02d}-{calendar.monthrange(ano, mes)[1]:02d}")


def faturamento_mensal(ano: int, mes: int) -> dict:
    return faturamento_periodo(*_intervalo_mes(ano, mes))


PERIODOS_FATURAMENTO = (
    ("este_mes", "Este mês"),
    ("mes_anterior", "Mês anterior"),
    ("este_ano", "Este ano"),
    ("personalizado", "Período personalizado"),
)


def intervalo_periodo(chave: str, hoje: date,
                      inicio: str | None = None,
                      fim: str | None = None) -> tuple[str, str]:
    if chave == "mes_anterior":
        ano, mes = (hoje.year - 1, 12) if hoje.month == 1 else (hoje.year, hoje.month - 1)
        return _intervalo_mes(ano, mes)
    if chave == "este_ano":
        return f"{hoje.year}-01-01", f"{hoje.year}-12-31"
    if chave == "hoje":
        return hoje.isoformat(), hoje.isoformat()
    if chave == "semana":
        seg = hoje - timedelta(days=hoje.weekday())
        return seg.isoformat(), (seg + timedelta(days=6)).isoformat()
    if chave == "personalizado":
        try:
            d_ini = date.fromisoformat(inicio or "")
            d_fim = date.fromisoformat(fim or "")
        except ValueError:
            raise ValueError("Informe datas válidas para o período personalizado.")
        if d_ini > d_fim:
            raise ValueError("A data inicial deve ser anterior à data final.")
        return d_ini.isoformat(), d_fim.isoformat()
    return _intervalo_mes(hoje.year, hoje.month)


# ---------------------------------------------------------------------------
# Dashboard — camada de consulta (sem tabelas próprias)
# ---------------------------------------------------------------------------

# Etapas da esteira do Dashboard agrupando o status operacional real.
ETAPAS_ESTEIRA = (
    ("confirmados", "Confirmados", ("preparacao",)),
    ("em_preparacao", "Em preparação", ("separado", "montado")),
    ("em_entrega", "Em entrega", ("entregue",)),
    ("finalizados", "Finalizados", ("recolhido", "conferido")),
)
STATUS_ATIVOS = ("preparacao", "separado", "montado", "entregue")
DIAS_ORCAMENTO_SEM_RETORNO = 3


def _hoje_iso() -> str:
    return formato.agora()[:10]


def indicadores_dashboard() -> dict:
    hoje = _hoje_iso()
    inicio_mes = hoje[:8] + "01"
    # mesma fonte da Esteira: pedidos atuais em operação, sem históricos
    with conectar() as conn:
        esteira = _pedidos_da_esteira(conn, hoje)
    kpis = indicadores_esteira(hoje, esteira)
    pedidos_ativos = kpis["na_esteira"]
    pedidos_em_preparacao = sum(1 for p in esteira
                                if p["status_operacional"] in ETAPAS_ESTEIRA[1][2])
    with conectar() as conn:
        eventos_hoje = conn.execute(
            "SELECT COUNT(*) FROM pedidos WHERE status_comercial != 'cancelado'"
            f" AND data_evento = ? AND {_t()}", (hoje,)).fetchone()[0]
        eventos_mes = conn.execute(
            "SELECT COUNT(*) FROM pedidos WHERE status_comercial != 'cancelado'"
            f" AND data_evento >= ? AND data_evento <= ? AND {_t()}",
            (inicio_mes, hoje[:8] + "31")).fetchone()[0]
        clientes_total = conn.execute(
            f"SELECT COUNT(*) FROM clientes WHERE status='ativo' AND {_t()}"
        ).fetchone()[0]
        clientes_novos_mes = conn.execute(
            "SELECT COUNT(*) FROM clientes WHERE status='ativo'"
            f" AND criado_em >= ? AND {_t()}", (inicio_mes,)).fetchone()[0]
    return {
        "pedidos_ativos": pedidos_ativos,
        "pedidos_em_preparacao": pedidos_em_preparacao,
        "pedidos_atrasados": kpis["atrasados"],
        "entregas_hoje": kpis["entregas_dia"],
        "eventos_hoje": eventos_hoje,
        "eventos_mes": eventos_mes,
        "clientes_total": clientes_total,
        "clientes_novos_mes": clientes_novos_mes,
    }


def faturamento_anual(ano: int) -> list:
    registros = faturamento_periodo(f"{ano}-01-01", f"{ano}-12-31")["registros"]
    meses = [{"mes": m, "total": 0.0, "quantidade": 0} for m in range(1, 13)]
    for r in registros:
        m = meses[int(r["data"][5:7]) - 1]
        m["quantidade"] += 1
        m["total"] += r["valor"] or 0
    return meses

def agenda_do_dia(data: str | None = None) -> list:
    data = data or _hoje_iso()
    with conectar() as conn:
        rows = conn.execute(
            "SELECT p.id, p.data_evento, p.data_retirada, p.data_devolucao,"
            " p.status_operacional, c.nome AS cliente_nome"
            " FROM pedidos p LEFT JOIN clientes c ON c.id = p.cliente_id"
            f" WHERE {_em_operacao()}"
            " AND (p.data_retirada = ? OR p.data_evento = ?"
            "      OR p.data_devolucao = ?)"
            " ORDER BY c.nome", (data, data, data)).fetchall()
    itens = []
    for ordem, (campo, tipo) in enumerate((("data_retirada", "retirada"),
                                            ("data_evento", "evento"),
                                            ("data_devolucao", "devolucao"))):
        for r in rows:
            if r[campo] == data:
                itens.append({"pedido_id": r["id"], "tipo": tipo,
                              "cliente_nome": r["cliente_nome"] or "",
                              "status_operacional": r["status_operacional"],
                              "_ordem": ordem})
    itens.sort(key=lambda x: x.pop("_ordem"))
    return itens


def agenda_proximos_dias(dias: int = 7, inicio: str | None = None) -> list:
    """Quantidade de retiradas, eventos e devoluções por dia, a partir de hoje."""
    from datetime import date, timedelta
    primeiro = date.fromisoformat(inicio or _hoje_iso())
    datas = [(primeiro + timedelta(days=i)).isoformat() for i in range(dias)]
    periodo = (datas[0], datas[-1])
    with conectar() as conn:
        rows = conn.execute(
            "SELECT d, tipo, COUNT(*) AS n FROM ("
            " SELECT data_retirada AS d, 'retiradas' AS tipo FROM pedidos"
            f"  WHERE {_em_operacao('')}"
            "  AND data_retirada BETWEEN ? AND ?"
            " UNION ALL"
            " SELECT data_evento, 'eventos' FROM pedidos"
            f"  WHERE {_em_operacao('')}"
            "  AND data_evento BETWEEN ? AND ?"
            " UNION ALL"
            " SELECT data_devolucao, 'devolucoes' FROM pedidos"
            f"  WHERE {_em_operacao('')}"
            "  AND data_devolucao BETWEEN ? AND ?"
            ") GROUP BY d, tipo", periodo * 3).fetchall()
    por_dia = {d: {"data": d, "dia_semana": date.fromisoformat(d).weekday(),
                   "retiradas": 0, "eventos": 0, "devolucoes": 0}
               for d in datas}
    for r in rows:
        por_dia[r["d"]][r["tipo"]] = r["n"]
    for dia in por_dia.values():
        dia["total"] = dia["retiradas"] + dia["eventos"] + dia["devolucoes"]
    return [por_dia[d] for d in datas]


def esteira_pedidos(limite_por_etapa: int = 5) -> list:
    """Resumo da Esteira para o Dashboard (mesmos pedidos e regras da Esteira)."""
    hoje = _hoje_iso()
    with conectar() as conn:
        pedidos = [p for p in _pedidos_da_esteira(conn, hoje)
                   if p["status_operacional"] != "finalizado"]
    etapas = []
    for chave, rotulo, status in ETAPAS_ESTEIRA:
        grupo = sorted((p for p in pedidos if p["status_operacional"] in status),
                       key=lambda p: (p["data_evento"] or p["data_retirada"] or "9999",
                                      p["id"]))
        etapas.append({"chave": chave, "rotulo": rotulo, "status": list(status),
                       "total": len(grupo),
                       "pedidos": [{"id": p["id"], "data_evento": p["data_evento"],
                                    "status_operacional": p["status_operacional"],
                                    "cliente_nome": p["cliente_nome"]}
                                   for p in grupo[:limite_por_etapa]]})
    return etapas


def alertas_dashboard() -> list:
    from datetime import date, timedelta
    hoje = _hoje_iso()
    dias = int(parametro("dias_orcamento_sem_retorno",
                         str(DIAS_ORCAMENTO_SEM_RETORNO)))
    limite = (date.fromisoformat(hoje) - timedelta(days=dias)).isoformat()
    with conectar() as conn:
        # regra única de atraso (a mesma da Esteira): com o cliente e a
        # devolução já deveria ter sido registrada
        devolucoes_atrasadas = sum(
            1 for p in _pedidos_da_esteira(conn, hoje)
            if p["status_operacional"] == "entregue"
            and situacao_prazo(p, hoje) == "atrasado")
        orcamentos_sem_retorno = conn.execute(
            "SELECT COUNT(*) FROM orcamentos"
            f" WHERE status='enviado' AND atualizado_em < ? AND {_t()}",
            (limite,)).fetchone()[0]
    alertas = []
    if devolucoes_atrasadas:
        alertas.append({"tipo": "devolucao_atrasada",
                        "quantidade": devolucoes_atrasadas,
                        "nivel": "critico"})
    if orcamentos_sem_retorno:
        alertas.append({"tipo": "orcamento_sem_retorno",
                        "quantidade": orcamentos_sem_retorno,
                        "nivel": "atencao", "dias": dias})
    return alertas


# ---------------------------------------------------------------------------
# Catalogo publico
# ---------------------------------------------------------------------------

def catalogo_produtos(categoria_id: int | None = None,
                      busca: str | None = None) -> list:
    sql = ("SELECT p.*, c.nome AS categoria_nome"
           " FROM produtos p LEFT JOIN categorias c ON c.id = p.categoria_id"
           f" WHERE p.status = 'disponivel' AND p.publicado = 1 AND {_t('p')}")
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
    sql = (f"SELECT * FROM kits WHERE status = 'ativo' AND publicado = 1"
           f" AND {_t()} ORDER BY nome")
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
            " WHERE p.id = ? AND p.status = 'disponivel' AND p.publicado = 1"
            f" AND {_t('p')}",
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
            "SELECT * FROM kits WHERE id = ? AND status = 'ativo' AND publicado = 1"
            f" AND {_t()}",
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
            f"SELECT * FROM origens_lead WHERE {_t()} ORDER BY nome").fetchall()]


def salvar_origem(nome: str, id_: int | None = None) -> int:
    nome = nome.strip()
    if not nome:
        raise ErroDeCampo("nome", "Nome da origem é obrigatório.")
    with conectar() as conn:
        dup = conn.execute(
            f"SELECT id FROM origens_lead WHERE nome = ? AND id != ? AND {_t()}",
            (nome, id_ or 0)).fetchone()
        if dup:
            raise ErroDeCampo("nome", "Já existe uma origem com este nome.")
        if id_:
            if not _do_tenant(conn, "origens_lead", id_):
                raise ErroDeCampo("nome", "Origem não encontrada.")
            conn.execute(f"UPDATE origens_lead SET nome=? WHERE id=? AND {_t()}",
                         (nome, id_))
            return id_
        r = conn.execute("INSERT INTO origens_lead (nome, tenant_id) VALUES (?, ?)",
                         (nome, tenant_atual()))
        return r.lastrowid


def excluir_origem(id_: int):
    with conectar() as conn:
        if not _do_tenant(conn, "origens_lead", id_):
            raise ValueError("Origem não encontrada.")
        em_uso = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE origem_id = ?",
            (id_,)).fetchone()[0]
        if em_uso:
            raise ValueError("Origem em uso por leads, não pode ser excluída.")
        conn.execute(f"DELETE FROM origens_lead WHERE id = ? AND {_t()}", (id_,))


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def _enriquecer_lead(conn, lead: dict) -> dict:
    if lead.get("cliente_id"):
        cli = conn.execute(f"SELECT nome FROM clientes WHERE id=? AND {_t()}",
                           (lead["cliente_id"],)).fetchone()
        lead["cliente_nome"] = cli["nome"] if cli else ""
    else:
        lead["cliente_nome"] = ""
    if lead.get("origem_id"):
        ori = conn.execute(f"SELECT nome FROM origens_lead WHERE id=? AND {_t()}",
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
    sql = f"SELECT * FROM leads WHERE {_t()}"
    params: list = []
    if status:
        sql += " AND status = ?"
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
            f"SELECT * FROM leads WHERE {_t()} ORDER BY atualizado_em DESC").fetchall()
        for r in rows:
            ld = dict(r)
            _enriquecer_lead(conn, ld)
            if ld["status"] in resultado:
                resultado[ld["status"]].append(ld)
    return resultado


def buscar_lead(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(f"SELECT * FROM leads WHERE id = ? AND {_t()}",
                         (id_,)).fetchone()
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
        _validar_referencias(conn, dados_)
        if id_:
            if not _do_tenant(conn, "leads", id_):
                raise ValueError("Lead não encontrado.")
            conn.execute(
                "UPDATE leads SET cliente_id=?, origem_id=?, interesse=?,"
                " valor_estimado=?, responsavel_id=?, status=?,"
                " data_evento=?, observacoes=?, atualizado_em=?"
                f" WHERE id=? AND {_t()}",
                (dados_["cliente_id"], dados_.get("origem_id"),
                 dados_.get("interesse", ""), dados_.get("valor_estimado", 0),
                 dados_.get("responsavel_id"), status,
                 dados_.get("data_evento"), dados_.get("observacoes", ""),
                 agora_, id_))
            return id_
        r = conn.execute(
            "INSERT INTO leads (tenant_id, cliente_id, origem_id, interesse,"
            " valor_estimado, responsavel_id, status, data_evento,"
            " observacoes, criado_em, atualizado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_atual(), dados_["cliente_id"], dados_.get("origem_id"),
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
        cur = conn.execute(
            f"UPDATE leads SET status=?, atualizado_em=? WHERE id=? AND {_t()}",
            (novo_status, agora_, id_))
        if not cur.rowcount:
            raise ValueError("Lead não encontrado.")


def contadores_lead() -> dict:
    with conectar() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) as qtd FROM leads WHERE {_t()} GROUP BY status"
        ).fetchall()
        return {r["status"]: r["qtd"] for r in rows}


# ---------------------------------------------------------------------------
# Orcamentos
# ---------------------------------------------------------------------------

def listar_orcamentos(status: str | None = None) -> list:
    sql = ("SELECT o.*, c.nome AS cliente_nome FROM orcamentos o"
           f" LEFT JOIN clientes c ON c.id = o.cliente_id WHERE {_t('o')}")
    params: list = []
    if status:
        sql += " AND o.status = ?"
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
            f" WHERE o.id = ? AND {_t('o')}", (id_,)).fetchone()
        if not r:
            return None
        orc = dict(r)
        orc["itens"] = _itens_orcamento(conn, orc["id"])
        orc["subtotal"] = sum(
            i["quantidade"] * i["preco_unitario"] for i in orc["itens"])
        orc["total"] = max(orc["subtotal"] - (orc["desconto"] or 0), 0)
        if orc.get("lead_id"):
            ld = conn.execute(f"SELECT interesse FROM leads WHERE id=? AND {_t()}",
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
        _validar_referencias(conn, dados_)
        _validar_itens_da_empresa(conn, itens)
        if id_:
            if not _do_tenant(conn, "orcamentos", id_):
                raise ValueError("Orçamento não encontrado.")
            conn.execute(
                "UPDATE orcamentos SET lead_id=?, cliente_id=?, desconto=?,"
                f" observacoes=?, status=?, atualizado_em=? WHERE id=? AND {_t()}",
                (dados_.get("lead_id"), dados_["cliente_id"], desconto,
                 dados_.get("observacoes", ""), status, agora_, id_))
            conn.execute("DELETE FROM itens_orcamento WHERE orcamento_id=?",
                         (id_,))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO orcamentos (tenant_id, lead_id, cliente_id, desconto,"
                " observacoes, status, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (tenant_atual(), dados_.get("lead_id"), dados_["cliente_id"], desconto,
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
    conds: list[str] = [_t("p")]
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
            ped["total"] = total_do_pedido(ped)
        return peds


# Nome exibido do responsável: o do usuário vinculado (acompanha mudanças de
# nome); sem vínculo, o texto guardado no pedido.
_SQL_NOME_RESPONSAVEL = "COALESCE(u.nome, NULLIF(TRIM(p.responsavel), ''), '')"


def buscar_pedido_festas(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT p.*, c.nome AS cliente_nome,"
            f" {_SQL_NOME_RESPONSAVEL} AS responsavel_nome FROM pedidos p"
            " LEFT JOIN clientes c ON c.id = p.cliente_id"
            " LEFT JOIN usuarios u ON u.id = p.responsavel_id"
            f" WHERE p.id = ? AND {_t('p')}", (id_,)).fetchone()
        if not r:
            return None
        ped = dict(r)
        ped["itens"] = [dict(r) for r in conn.execute(
            "SELECT * FROM itens_pedido WHERE pedido_id=? ORDER BY id",
            (ped["id"],)).fetchall()]
        ped["total"] = total_do_pedido(ped)
        return ped


def converter_orcamento_em_pedido(orcamento_id: int, usuario_id=None) -> int:
    orc = buscar_orcamento(orcamento_id)
    if not orc:
        raise ValueError("Orçamento não encontrado.")
    if orc["status"] == "recusado":
        raise ValueError("Orçamento recusado não pode ser convertido.")

    agora_ = formato.agora()
    with conectar() as conn:
        r = conn.execute(
            "INSERT INTO pedidos (tenant_id, orcamento_id, cliente_id, data_evento,"
            " observacoes, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?)",
            (tenant_atual(), orcamento_id, orc["cliente_id"], None,
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
            f" WHERE id=? AND {_t()}", (agora_, orcamento_id))
        if orc.get("lead_id"):
            conn.execute(
                "UPDATE leads SET status='contratado', atualizado_em=?"
                f" WHERE id=? AND {_t()}", (agora_, orc["lead_id"]))
        registrar_evento_pedido(
            conn, pedido_id, "Pedido criado", "Comercial", usuario_id,
            detalhe=f"Convertido do orçamento #{orcamento_id}.")
        return pedido_id


def campos_pedido_festas(form) -> dict:
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    d = {
        "cliente_id": _int(form.get("cliente_id")),
        "data_evento": (form.get("data_evento") or "").strip() or None,
        "data_retirada": (form.get("data_retirada") or "").strip() or None,
        "data_devolucao": (form.get("data_devolucao") or "").strip() or None,
        "status_comercial": form.get("status_comercial") or None,
        "status_operacional": form.get("status_operacional") or None,
        "observacoes": (form.get("observacoes") or "").strip(),
        "versao": (form.get("versao") or "").strip() or None,
    }
    for campo in CAMPOS_TEXTO_PEDIDO:
        d[campo] = (form.get(campo) or "").strip()
    # O responsável vem só da lista de usuários; o texto livre do formulário
    # é ignorado. Formulário sem o campo (aberto antes da atualização) mantém
    # o que já estava no pedido.
    d["responsavel_id"] = (form.get("responsavel_id", "manter") or "").strip()
    return d


def _resolver_responsavel(conn, dados_: dict, atual: dict | None):
    """Define responsavel_id e o nome guardado a partir da escolha na lista.

    "manter" preserva o que o pedido já tinha (inclusive um nome digitado antes
    da lista ou um usuário depois desativado); um id precisa ser de usuário
    ativo da empresa atual. Chamadas internas sem o campo mantêm o vínculo."""
    atual = atual or {}
    if "responsavel_id" not in dados_:
        dados_["responsavel_id"] = atual.get("responsavel_id")
        return
    escolha = str(dados_.get("responsavel_id") or "").strip()
    if escolha == "manter":
        dados_["responsavel_id"] = atual.get("responsavel_id")
        dados_["responsavel"] = atual.get("responsavel") or ""
        return
    if not escolha:
        dados_["responsavel_id"], dados_["responsavel"] = None, ""
        return
    try:
        uid = int(escolha)
    except ValueError:
        raise ErroDeCampo("responsavel_id", "Responsável inválido.")
    r = conn.execute(
        "SELECT u.nome, u.ativo = 1 AND m.ativo = 1 AS ativo FROM membros m"
        f" JOIN usuarios u ON u.id = m.usuario_id WHERE m.usuario_id = ? AND {_t('m')}",
        (uid,)).fetchone()
    if not r or (not r["ativo"] and uid != atual.get("responsavel_id")):
        raise ErroDeCampo("responsavel_id", "Responsável não encontrado entre os usuários ativos.")
    if uid == atual.get("responsavel_id"):
        dados_["responsavel_id"] = uid
        dados_["responsavel"] = atual.get("responsavel") or r["nome"]
        return
    dados_["responsavel_id"], dados_["responsavel"] = uid, r["nome"]


def opcoes_responsavel(pedido: dict | None) -> dict:
    """Opções do campo Responsável no formulário do pedido.

    Um nome digitado antes da lista é pré-selecionado no usuário de nome
    idêntico (só se houver exatamente um); senão aparece como "anterior" e
    fica como está até alguém escolher outro."""
    pedido = pedido or {}
    usuarios = [(u["id"], u["nome"]) for u in listar_usuarios()]
    rid = pedido.get("responsavel_id")
    texto = (pedido.get("responsavel") or "").strip()
    escolhido, anterior = "", ""
    if rid and any(i == rid for i, _ in usuarios):
        escolhido = str(rid)
    elif rid:
        escolhido = "manter"
        anterior = f"{pedido.get('responsavel_nome') or texto} (usuário inativo)"
    elif texto:
        chave = normalizar_texto(texto).split()
        iguais = [i for i, n in usuarios if normalizar_texto(n).split() == chave]
        if len(iguais) == 1:
            escolhido = str(iguais[0])
        else:
            escolhido, anterior = "manter", f"{texto} (registrado antes)"
    return {"usuarios": usuarios, "escolhido": escolhido, "anterior": anterior}


def _validar_itens(itens: list) -> list:
    validos = []
    for n, item in enumerate(itens):
        descricao = (item.get("descricao") or "").strip()
        if not descricao:
            continue
        campo = f"item_descricao_{n}"
        tipo = item.get("tipo") or "produto"
        if tipo not in TIPOS_ITEM:
            raise ErroDeCampo(campo, f"Tipo de item inválido: {tipo}.")
        try:
            qtd = int(item.get("quantidade") or 1)
            preco = float(str(item.get("preco_unitario") or 0).replace(",", "."))
        except ValueError:
            raise ErroDeCampo(campo, f"Quantidade ou valor inválido em '{descricao}'.")
        if qtd < 1:
            raise ErroDeCampo(campo, f"Quantidade de '{descricao}' deve ser ao menos 1.")
        if preco < 0:
            raise ErroDeCampo(campo, f"Valor de '{descricao}' não pode ser negativo.")
        validos.append({"tipo": tipo, "item_id": item.get("item_id"),
                        "descricao": descricao, "quantidade": qtd,
                        "preco_unitario": round(preco, 2)})
    return validos


def _validar_dados_pedido(d: dict):
    for campo in ("data_evento", "data_retirada", "data_devolucao"):
        if d.get(campo):
            try:
                date.fromisoformat(d[campo])
            except ValueError:
                raise ErroDeCampo(campo, "Data inválida.")
    for campo in ("hora_retirada", "hora_devolucao"):
        if d.get(campo) and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", d[campo]):
            raise ErroDeCampo(campo, "Horário inválido (use HH:MM).")
    for campo in CAMPOS_TEXTO_PEDIDO:
        if len(d.get(campo) or "") > 200:
            raise ErroDeCampo(campo, "Texto muito longo (máximo 200 caracteres).")
    if d.get("canal") and d["canal"] not in CANAIS:
        raise ErroDeCampo("canal", "Canal inválido.")
    if (d.get("data_retirada") and d.get("data_devolucao")
            and d["data_retirada"] > d["data_devolucao"]):
        raise ErroDeCampo("data_devolucao",
                          "Data de devolução deve ser posterior à retirada.")
    # Sprint 4: datas coerentes com a festa (a Agenda aponta os casos antigos)
    ev = d.get("data_evento")
    if ev and d.get("data_retirada") and d["data_retirada"] > ev:
        raise ErroDeCampo("data_retirada",
                          "Retirada/entrega não pode ser depois do evento.")
    if ev and d.get("data_devolucao") and d["data_devolucao"] < ev:
        raise ErroDeCampo("data_devolucao", "Devolução não pode ser antes do evento.")
    if (d.get("data_retirada") and d.get("data_retirada") == d.get("data_devolucao")
            and d.get("hora_retirada") and d.get("hora_devolucao")
            and d["hora_devolucao"] <= d["hora_retirada"]):
        raise ErroDeCampo("hora_devolucao",
                          "No mesmo dia, a devolução precisa ser depois da saída.")


def _validar_itens_da_empresa(conn, itens: list):
    """Produto ou kit citado num item precisa ser da empresa atual."""
    for item in itens:
        tabela = {"produto": "produtos", "kit": "kits"}.get(item.get("tipo"))
        if tabela and item.get("item_id") and not _do_tenant(
                conn, tabela, item["item_id"]):
            raise ErroDeCampo("item_descricao_0",
                              f"Item não encontrado: {item.get('descricao', '')}.")


def _assinatura_itens(itens: list) -> list:
    return sorted(f"{i['tipo']}:{i['descricao']} x{int(i['quantidade'])}"
                  f" @ {float(i['preco_unitario']):.2f}" for i in itens)



def _verificar_disponibilidade_itens(conn, itens: list, data_retirada: str,
                                     data_devolucao: str,
                                     pedido_id: int | None = None):
    faltas = _faltas_de_estoque(conn, itens, data_retirada, data_devolucao, pedido_id)
    if faltas:
        raise ErroDeCampo("item_descricao_0", faltas[0])


def _faltas_de_estoque(conn, itens: list, data_retirada: str, data_devolucao: str,
                       pedido_id: int | None = None) -> list:
    """Produtos do pedido sem quantidade livre no período (mensagens)."""
    faltas = []
    for item in itens:
        if item.get("tipo") != "produto" or not item.get("item_id"):
            continue
        pid = item["item_id"]
        r = conn.execute(
            f"SELECT quantidade_total, status, nome FROM produtos WHERE id=? AND {_t()}",
            (pid,)).fetchone()
        if not r:
            faltas.append("Produto não encontrado.")
            continue
        if r["status"] == "manutencao":
            faltas.append(f"Produto '{r['nome']}' esta em manutencao.")
            continue
        total = r["quantidade_total"]
        sql = (
            "SELECT COALESCE(SUM(ip.quantidade), 0) FROM itens_pedido ip"
            " JOIN pedidos p ON p.id = ip.pedido_id"
            " WHERE ip.tipo = 'produto' AND ip.item_id = ?"
            f" AND {_em_operacao()} AND {_t('p')}"
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
            faltas.append(f"Quantidade insuficiente para '{r['nome']}' nesta data."
                          f" Disponivel: {livre}, solicitado: {qtd}.")
    return faltas


def salvar_pedido_festas(dados_: dict, itens: list, id_: int | None = None,
                         usuario_id=None, pode_alterar_status: bool = True) -> int:
    """Cria ou edita um pedido com validação no servidor e registro das mudanças.

    pode_alterar_status=False (perfis não administradores) mantém os status
    atuais; status mudam pelas ações do pedido (avançar, finalizar, cancelar).
    """
    if not dados_.get("cliente_id"):
        raise ErroDeCampo("cliente_id", "Cliente é obrigatório.")
    _validar_dados_pedido(dados_)
    itens = _validar_itens(itens)

    atual = None
    with conectar() as conn:
        if not _do_tenant(conn, "clientes", dados_["cliente_id"]):
            raise ErroDeCampo("cliente_id", "Cliente não encontrado.")
        _validar_itens_da_empresa(conn, itens)
        if id_:
            r = conn.execute(f"SELECT * FROM pedidos WHERE id = ? AND {_t()}",
                             (id_,)).fetchone()
            if not r:
                raise ValueError("Pedido não encontrado.")
            atual = dict(r)
            atual["itens"] = [dict(i) for i in conn.execute(
                "SELECT * FROM itens_pedido WHERE pedido_id = ?", (id_,))]
        _resolver_responsavel(conn, dados_, atual)

    if atual:
        if dados_.get("versao") and dados_["versao"] != atual["atualizado_em"]:
            raise ErroDeCampo(
                "versao", "Este pedido foi alterado por outra pessoa enquanto você"
                " editava. Recarregue a página para ver a versão atual.")
        if atual["historico"] and not pode_alterar_status:
            raise ErroDeCampo(
                "versao", "Pedido histórico: somente um administrador pode corrigir.")
        if atual["status_comercial"] in FORA_DA_OPERACAO and not pode_alterar_status:
            raise ErroDeCampo(
                "versao", f"Pedido {atual['status_comercial']} não pode ser editado.")
        if not pode_alterar_status:
            dados_["status_comercial"] = atual["status_comercial"]
            dados_["status_operacional"] = atual["status_operacional"]

    sc = dados_.get("status_comercial") or (atual or {}).get("status_comercial") or "confirmado"
    so = dados_.get("status_operacional") or (atual or {}).get("status_operacional") or "preparacao"
    if not pode_alterar_status and not atual:
        sc, so = "confirmado", "preparacao"
    if sc not in STATUS_PEDIDO_COMERCIAL:
        raise ErroDeCampo("status_comercial", "Status comercial inválido.")
    if so not in STATUS_PEDIDO_OPERACIONAL:
        raise ErroDeCampo("status_operacional", "Status operacional inválido.")

    data_ret = dados_.get("data_retirada")
    data_dev = dados_.get("data_devolucao")
    agora_ = formato.agora()
    extras = [dados_.get(c, "") for c in CAMPOS_TEXTO_PEDIDO]
    # Histórico é pedido encerrado: se o administrador reabre o status,
    # a marca sai junto (Sprint 2.1).
    historico = int(bool(atual and atual["historico"] and sc in FORA_DA_OPERACAO))

    with conectar() as conn:
        if data_ret and data_dev and sc not in FORA_DA_OPERACAO:
            _verificar_disponibilidade_itens(conn, itens, data_ret, data_dev, id_)

        if atual:
            conn.execute(
                "UPDATE pedidos SET cliente_id=?, data_evento=?,"
                " data_retirada=?, data_devolucao=?,"
                " status_comercial=?, status_operacional=?, observacoes=?,"
                + "".join(f" {c}=?," for c in CAMPOS_TEXTO_PEDIDO) +
                f" responsavel_id=?, historico=?, atualizado_em=? WHERE id=? AND {_t()}",
                (dados_["cliente_id"], dados_.get("data_evento"), data_ret,
                 data_dev, sc, so, dados_.get("observacoes", ""),
                 *extras, dados_.get("responsavel_id"), historico, agora_, id_))
            conn.execute("DELETE FROM itens_pedido WHERE pedido_id=?", (id_,))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO pedidos (tenant_id, cliente_id, data_evento, data_retirada,"
                " data_devolucao, status_comercial, status_operacional,"
                " observacoes, "
                + "".join(f"{c}, " for c in CAMPOS_TEXTO_PEDIDO) +
                "responsavel_id, criado_em, atualizado_em) VALUES ("
                + ",".join("?" * (11 + len(CAMPOS_TEXTO_PEDIDO))) + ")",
                (tenant_atual(), dados_["cliente_id"], dados_.get("data_evento"), data_ret,
                 data_dev, sc, so, dados_.get("observacoes", ""),
                 *extras, dados_.get("responsavel_id"), agora_, agora_))
            novo_id = r.lastrowid

        for item in itens:
            conn.execute(
                "INSERT INTO itens_pedido (pedido_id, tipo, item_id,"
                " descricao, quantidade, preco_unitario) VALUES (?,?,?,?,?,?)",
                (novo_id, item["tipo"], item.get("item_id"), item["descricao"],
                 item["quantidade"], item["preco_unitario"]))

        if atual:
            novos = dict(dados_, status_comercial=sc, status_operacional=so,
                         data_retirada=data_ret, data_devolucao=data_dev,
                         historico=historico)
            mudancas = {}
            for campo in ("cliente_id", "data_evento", "data_retirada",
                          "data_devolucao", "status_comercial",
                          "status_operacional", "observacoes", "historico",
                          "responsavel_id", *CAMPOS_TEXTO_PEDIDO):
                antes, depois = atual.get(campo) or "", novos.get(campo) or ""
                if str(antes) != str(depois):
                    mudancas[campo] = [antes, depois]
            if _assinatura_itens(atual["itens"]) != _assinatura_itens(itens):
                mudancas["itens"] = [_assinatura_itens(atual["itens"]),
                                     _assinatura_itens(itens)]
            if mudancas:
                status = {k for k in mudancas if k.startswith("status_")}
                registrar_evento_pedido(
                    conn, novo_id,
                    "Status corrigido pelo administrador" if status else "Pedido editado",
                    "Comercial", usuario_id,
                    detalhe=", ".join(sorted({ROTULOS_CAMPO_PEDIDO.get(k, k)
                                              for k in mudancas})),
                    mudancas=mudancas)
        else:
            registrar_evento_pedido(conn, novo_id, "Pedido criado", "Comercial",
                                    usuario_id)

    atualizar_classificacao_cliente(int(dados_["cliente_id"]))
    if atual and atual["cliente_id"] != dados_["cliente_id"]:
        atualizar_classificacao_cliente(atual["cliente_id"])
    return novo_id


ROTULOS_CAMPO_PEDIDO = {
    "cliente_id": "cliente", "data_evento": "data do evento",
    "data_retirada": "retirada", "data_devolucao": "devolução",
    "status_comercial": "status comercial",
    "status_operacional": "status operacional", "observacoes": "observações",
    "itens": "itens e valores", "local_evento": "local",
    "hora_retirada": "horário da retirada", "hora_devolucao": "horário da devolução",
    "responsavel": "responsável", "responsavel_id": "responsável",
    "forma_pagamento": "forma de pagamento",
    "condicao_pagamento": "condição de pagamento", "canal": "canal",
    "historico": "marca de histórico",
}


# Sprint 3: Preparação → Separado → Entregue/Retirado → Recolhido/Devolvido
# → Conferência → Finalizado. "montado" é um valor antigo, mantido nos dados:
# fica na coluna Separado e segue direto para a entrega.
FLUXO_OPERACIONAL = {
    "preparacao": "separado",
    "separado": "entregue",
    "montado": "entregue",
    "entregue": "recolhido",
    "recolhido": "conferido",
}


def listar_pedidos_operacional(status_operacional: str | None = None,
                               busca: str | None = None) -> list:
    sql = ("SELECT p.*, c.nome AS cliente_nome FROM pedidos p"
           " LEFT JOIN clientes c ON c.id = p.cliente_id"
           f" WHERE {_em_operacao()}")
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
            ped["total"] = total_do_pedido(ped)
        if busca:
            b = busca.lower()
            peds = [p for p in peds
                    if b in (p.get("cliente_nome") or "").lower()
                    or b in str(p.get("id", ""))]
        return peds


def avancar_status_operacional(pedido_id: int, observacao: str = "",
                               usuario_id=None) -> str:
    """Avança uma etapa (a seguinte do fluxo). Mesma regra da esteira."""
    with conectar() as conn:
        ped = conn.execute(
            "SELECT status_comercial, status_operacional, historico FROM pedidos"
            f" WHERE id=? AND {_t()}", (pedido_id,)).fetchone()
    if not ped:
        raise ValueError("Pedido não encontrado.")
    _verificar_operavel(ped)
    destino = FLUXO_OPERACIONAL.get(ped["status_operacional"])
    if not destino:
        raise ValueError(f"Status '{ped['status_operacional']}' não pode avançar.")
    return mover_etapa(pedido_id, destino, usuario_id, observacao=observacao)


def _verificar_operavel(ped) -> None:
    """Histórico, cancelado e finalizado não se movem na operação."""
    if ped["historico"]:
        raise ValueError("Pedido histórico não gera operação.")
    if ped["status_comercial"] == "cancelado" or ped["status_operacional"] == "cancelado":
        raise ValueError("Pedido cancelado não pode avançar.")
    if ped["status_comercial"] == "finalizado" or ped["status_operacional"] == "finalizado":
        raise ValueError("Pedido finalizado não pode avançar.")


def mover_etapa(pedido_id: int, destino: str, usuario_id=None,
                esperado: str | None = None, observacao: str = "") -> str:
    """Única porta de mudança de etapa operacional (botão, arrastar, detalhe).

    Valida o estado atual no servidor: histórico, cancelado e finalizado não
    se movem; só vale a etapa seguinte do fluxo (sem pular, sem voltar);
    `esperado` recusa a ação se o pedido mudou de etapa enquanto a tela
    estava aberta. Finalizar exige a conferência.
    """
    with conectar() as conn:
        ped = conn.execute(
            "SELECT status_comercial, status_operacional, historico FROM pedidos"
            f" WHERE id=? AND {_t()}", (pedido_id,)).fetchone()
        if not ped:
            raise ValueError("Pedido não encontrado.")
        _verificar_operavel(ped)
        atual = ped["status_operacional"]
        if esperado and esperado != atual:
            raise ValueError("Este pedido mudou de etapa enquanto a tela estava aberta."
                             " Atualize a esteira.")
        permitido = "finalizado" if atual == "conferido" else FLUXO_OPERACIONAL.get(atual)
        if destino != permitido:
            de = ROTULOS_OPERACIONAL.get(atual, atual)
            para = ROTULOS_OPERACIONAL.get(destino, destino)
            raise ValueError(f"Transição inválida: {de} → {para}.")
    if destino == "finalizado":
        finalizar_pedido(pedido_id, usuario_id)
        return destino
    with conectar() as conn:
        conn.execute(
            f"UPDATE pedidos SET status_operacional=?, atualizado_em=? WHERE id=? AND {_t()}",
            (destino, formato.agora(), pedido_id))
        registrar_evento_pedido(
            conn, pedido_id, TITULO_AVANCO.get(destino, f"Avançou para {destino}"),
            "Agenda" if destino == "entregue" else "Operação", usuario_id,
            detalhe=observacao.strip(),
            mudancas={"status_operacional": [atual, destino]})
    return destino


def cancelar_pedido(id_: int, motivo: str = "", usuario_id=None):
    with conectar() as conn:
        ped = conn.execute(
            "SELECT status_comercial, status_operacional, historico, cliente_id"
            f" FROM pedidos WHERE id=? AND {_t()}", (id_,)).fetchone()
        if not ped:
            raise ValueError("Pedido não encontrado.")
        if ped["status_comercial"] == "cancelado":
            raise ValueError("Pedido já está cancelado.")
        if ped["status_comercial"] == "finalizado" or ped["historico"]:
            raise ValueError("Pedido finalizado não pode ser cancelado.")
        conn.execute(
            "UPDATE pedidos SET status_comercial='cancelado',"
            " status_operacional='cancelado',"
            f" motivo_cancelamento=?, atualizado_em=? WHERE id=? AND {_t()}",
            (motivo, formato.agora(), id_))
        registrar_evento_pedido(
            conn, id_, "Pedido cancelado", "Comercial", usuario_id,
            detalhe=motivo.strip(),
            mudancas={"status_comercial": [ped["status_comercial"], "cancelado"],
                      "status_operacional": [ped["status_operacional"], "cancelado"]})
    atualizar_classificacao_cliente(ped["cliente_id"])

def eventos_agenda(data_inicio: str, data_fim: str,
                    tipo: str | None = None,
                    status_comercial: str | None = None) -> list:
    sql = ("SELECT p.id, p.cliente_id, p.data_evento, p.data_retirada,"
           " p.data_devolucao, p.status_comercial, p.status_operacional,"
           " p.observacoes, c.nome AS cliente_nome"
           " FROM pedidos p LEFT JOIN clientes c ON c.id = p.cliente_id"
           f" WHERE p.status_comercial != 'cancelado' AND {_t('p')}")
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
    conds: list[str] = [_historico_visivel()]
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
            " h.observacoes, canal_canonico(h.canal) AS canal, h.valor,"
            " h.status_origem, h.origem AS fonte, h.origem_id,"
            f" {_operacao_sql('h.origem')} AS origem, c.nome AS cliente_nome"
            " FROM eventos_historico h"
            " LEFT JOIN clientes c ON c.id = h.cliente_id"
            " WHERE h.data_evento >= ? AND h.data_evento <= ?"
            f" AND {_historico_visivel()}"
            " ORDER BY h.data_evento",
            (data_inicio, data_fim)).fetchall()
    return [dict(r) for r in rows]


def eventos_historico_cliente(cliente_id: int) -> list:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT id, data_evento, descricao, observacoes, canal, valor,"
            " status_origem, origem, origem_id,"
            f" {_operacao_sql('origem')} AS operacao,"
            " canal_canonico(canal) AS canal_padrao,"
            " situacao_historico(origem, status_origem, data_evento) AS situacao"
            f" FROM eventos_historico WHERE cliente_id = ? AND {_historico_visivel('')}"
            " ORDER BY data_evento DESC",
            (cliente_id,)).fetchall()
    return [dict(r) for r in rows]


def salvar_evento_historico(dados_evt: dict) -> int:
    with conectar() as conn:
        cur = conn.execute(
            "INSERT INTO eventos_historico"
            " (tenant_id, cliente_id, origem_id, origem, data_evento, descricao,"
            "  observacoes, canal, valor, status_origem)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tenant_atual(), dados_evt.get("cliente_id"),
             dados_evt["origem_id"],
             dados_evt.get("origem", "Morumbi 3D"),
             dados_evt.get("data_evento"),
             dados_evt.get("descricao", ""),
             dados_evt.get("observacoes", ""),
             dados_evt.get("canal", ""),
             dados_evt.get("valor", 0),
             dados_evt.get("status_origem", "")))
        return cur.lastrowid


def salvar_historico_manual(id_: int, valor: float | None = None,
                            data_evento: str | None = None,
                            usuario_id=None) -> dict:
    """Corrige à mão valor e/ou data de um registro importado.

    data_evento=None mantém a data atual. Origem e identificadores nunca mudam;
    cada campo alterado vira um registro de auditoria com antes e depois.
    """
    if data_evento is not None:
        try:
            data_evento = date.fromisoformat(data_evento).isoformat()
        except ValueError:
            raise ValueError("Data do evento inválida.")
    with conectar() as conn:
        r = conn.execute(
            "SELECT id, origem, origem_id, valor, data_evento"
            f" FROM eventos_historico WHERE id = ? AND {_t()}", (id_,)).fetchone()
        if not r:
            raise ValueError("Registro histórico não encontrado.")
        if r["origem"] in ORIGENS_SOMENTE_LEITURA:
            raise ValueError(f"Registros {r['origem']} vêm do próprio"
                             f" {r['origem']} e não são editados aqui.")
        mudancas = {}
        if (r["valor"] or None) != (valor or None):
            mudancas["valor"] = (r["valor"] or None, valor or None)
        if data_evento is not None and data_evento != r["data_evento"]:
            mudancas["data_evento"] = (r["data_evento"], data_evento)
        if not mudancas:
            return {"alterado": False, "mudancas": {}}

        agora_ = formato.agora()
        for campo, (antes, depois) in mudancas.items():
            conn.execute(f"UPDATE eventos_historico SET {campo} = ? WHERE id = ?"
                         f" AND {_t()}", (depois, id_))
            if campo == "valor":
                tipo = "valor_historico"
                texto = f"{formato.dinheiro(antes)} → {formato.dinheiro(depois)}"
            else:
                tipo = "data_historico"
                texto = f"{antes or 'sem data'} → {depois}"
            conn.execute(
                "INSERT INTO audit_log (tenant_id, usuario_id, tipo, descricao, dados, criado_em)"
                f" VALUES ({tenant_atual()}, ?, ?, ?, ?, ?)",
                (usuario_id, tipo,
                 f"{campo} de {r['origem']} #{r['origem_id']}: {texto}",
                 json.dumps({"id": id_, "origem": r["origem"],
                             "origem_id": r["origem_id"], "campo": campo,
                             "antes": antes, "depois": depois}),
                 agora_))
    atualizar_classificacao_cliente_do_historico(id_)
    return {"alterado": True, "mudancas": mudancas}


def salvar_valor_historico(id_: int, valor: float | None, usuario_id=None) -> dict:
    return salvar_historico_manual(id_, valor=valor, usuario_id=usuario_id)


def atualizar_classificacao_cliente_do_historico(id_: int):
    with conectar() as conn:
        r = conn.execute(f"SELECT cliente_id FROM eventos_historico WHERE id = ?"
                         f" AND {_t()}", (id_,)).fetchone()
    if r and r["cliente_id"]:
        atualizar_classificacao_cliente(r["cliente_id"])


def historicos_data_futura() -> list:
    """Importados finalizados com evento depois de hoje: quase sempre erro de digitação."""
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT h.id, h.origem, h.origem_id, h.data_evento, c.nome AS cliente_nome"
            " FROM eventos_historico h LEFT JOIN clientes c ON c.id = h.cliente_id"
            " WHERE situacao_historico(h.origem, h.status_origem, h.data_evento)"
            "       = 'finalizado'"
            f" AND {_historico_visivel()}"
            " AND h.data_evento > ? ORDER BY h.data_evento",
            (_hoje_iso(),)).fetchall()]


def pedidos_cliente(cliente_id: int) -> list:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT p.id, p.data_evento, p.data_retirada, p.data_devolucao,"
            " p.status_comercial, p.status_operacional, p.observacoes,"
            f" COALESCE({_total_pedido_sql()}, 0) AS valor_total"
            " FROM pedidos p"
            f" WHERE p.cliente_id = ? AND {_t('p')}"
            " ORDER BY p.data_evento DESC",
            (cliente_id,)).fetchall()
    return [dict(r) for r in rows]


def _festas_realizadas_sql() -> str:
    realizados = ",".join(f"'{s}'" for s in COMERCIAL_REALIZADO)
    return (
        "SELECT cliente_id, data_evento FROM eventos_historico"
        " WHERE situacao_historico(origem, status_origem, data_evento) = 'finalizado'"
        f" AND {_historico_visivel('')}"
        " UNION ALL"
        " SELECT cliente_id, data_evento FROM pedidos"
        f" WHERE status_comercial IN ({realizados}) AND {_t()}")


def atualizar_classificacao_cliente(cliente_id: int):
    """Festas realizadas: históricos finalizados + pedidos entregues/finalizados.

    Conta sempre dentro da empresa do próprio cliente.
    """
    with conectar() as conn:
        dono = conn.execute("SELECT tenant_id FROM clientes WHERE id = ?",
                            (cliente_id,)).fetchone()
    if not dono:
        return
    with usando_tenant(dono["tenant_id"]), conectar() as conn:
        r = conn.execute(
            f"SELECT COUNT(*), MAX(data_evento) FROM ({_festas_realizadas_sql()})"
            " WHERE cliente_id = ?", (cliente_id,)).fetchone()
        total, ultima = r[0], r[1]
        conn.execute(
            "UPDATE clientes SET total_festas = ?, classificacao = ?,"
            " ultima_festa = ? WHERE id = ?",
            (total, classificar_festas(total), ultima, cliente_id))


def atualizar_todas_classificacoes() -> int:
    with conectar() as conn:
        clientes = conn.execute(
            "SELECT DISTINCT cliente_id FROM ("
            f"  SELECT cliente_id FROM eventos_historico WHERE {_t()}"
            "  UNION"
            f"  SELECT cliente_id FROM pedidos WHERE {_t()}"
            "  UNION"
            f"  SELECT id FROM clientes WHERE total_festas > 0 AND {_t()}"
            ") WHERE cliente_id IS NOT NULL").fetchall()
    for row in clientes:
        atualizar_classificacao_cliente(row[0])
    return len(clientes)

def disponibilidade_calendario(produto_id: int, ano: int, mes: int) -> list:
    import calendar
    _, ultimo_dia = calendar.monthrange(ano, mes)
    resultado = []
    with conectar() as conn:
        r = conn.execute("SELECT quantidade_total, status FROM produtos"
                         f" WHERE id=? AND {_t()}", (produto_id,)).fetchone()
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
                f" AND {_em_operacao()} AND {_t('p')}"
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
            " p.observacoes, p.criado_em, p.historico, p.canal, p.fonte,"
            f" COALESCE({_total_pedido_sql()}, 0) AS total,"
            " (SELECT COUNT(*) FROM itens_pedido i WHERE i.pedido_id = p.id)"
            " AS itens_count"
            " FROM pedidos p"
            " LEFT JOIN clientes c ON c.id = p.cliente_id"
            f" WHERE {_t('p')}"
        ).fetchall()
        for p in peds:
            p = dict(p)
            resultado.append({
                "tipo": "pedido",
                "id": p["id"],
                "numero": p["id"],
                "cliente_nome": p["cliente_nome"],
                "cliente_id": p["cliente_id"],
                "data_evento": p["data_evento"],
                "itens_count": p["itens_count"],
                "total": p["total"],
                "origem": nome_empresa(),
                "fonte": p["fonte"] or "",
                "canal": p["canal"] or "",
                "historico": bool(p["historico"]),
                "status_comercial": p["status_comercial"],
                "status_operacional": p["status_operacional"],
                "data_retirada": p["data_retirada"],
                "data_devolucao": p["data_devolucao"],
                "criado_em": p["criado_em"],
                "observacoes": p.get("observacoes") or "",
            })

        hists = conn.execute(
            "SELECT h.id, h.cliente_id, c.nome AS cliente_nome,"
            " h.data_evento, h.descricao, h.valor, h.status_origem,"
            " h.origem, h.origem_id, h.canal, h.criado_em,"
            " situacao_historico(h.origem, h.status_origem, h.data_evento) AS situacao"
            " FROM eventos_historico h"
            " LEFT JOIN clientes c ON c.id = h.cliente_id"
            f" WHERE {_historico_visivel()}"
        ).fetchall()
        for h in hists:
            h = dict(h)
            situacao = h["situacao"]
            resultado.append({
                "tipo": "historico",
                "id": h["id"],
                "numero": h.get("origem_id") or h["id"],
                "cliente_nome": h["cliente_nome"],
                "cliente_id": h["cliente_id"],
                "data_evento": h["data_evento"],
                "itens_count": 1,
                "total": h.get("valor") or 0,
                "origem": operacao_da_fonte(h.get("origem")),
                "fonte": h.get("origem") or "",
                "canal": canal_canonico(h.get("canal")),
                "historico": situacao != "pendente",
                "status_origem": h.get("status_origem") or "",
                "status_comercial": situacao,
                "status_operacional": ("finalizado" if situacao == "finalizado"
                                       else "—"),
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
    """Operações com pedidos visíveis (o filtro Origem é de operação, não de fonte)."""
    with conectar() as conn:
        origens = [nome_empresa()]
        rows = conn.execute(
            f"SELECT DISTINCT {_operacao_sql('origem')} FROM eventos_historico"
            f" WHERE {_historico_visivel('')}").fetchall()
        for r in rows:
            if r[0] and r[0] not in origens:
                origens.append(r[0])
        return origens


def indicadores_pedidos() -> dict:
    from datetime import timedelta
    hoje = date.fromisoformat(_hoje_iso())
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
            f" WHERE status_comercial IN ('confirmado', 'entregue') AND {_t()}"
        ).fetchone()[0]

        proximos = conn.execute(
            "SELECT COUNT(*) FROM pedidos"
            " WHERE data_evento >= ? AND data_evento <= ?"
            f" AND status_comercial IN ('confirmado', 'entregue') AND {_t()}",
            (hoje.isoformat(), daqui_30),
        ).fetchone()[0]

        historico = conn.execute(
            f"SELECT COUNT(*) FROM eventos_historico WHERE {_historico_visivel('')}"
        ).fetchone()[0]

    fin = faturamento_mensal(ano, mes)
    return {
        "abertos": abertos,
        "proximos": proximos,
        "faturamento_total": fin["total"],
        "faturamento_qtd": fin["quantidade"],
        "historico": historico,
    }


# ---------------------------------------------------------------------------
# Normalização de pedidos históricos (Sprint 1)
#
# pedidos: festas com data anterior a DATA_CORTE_FINALIZADOS passam a
#   status_comercial = status_operacional = 'finalizado' e historico = 1.
#   Cancelados e pedidos com data igual ou posterior ao corte não mudam.
# eventos_historico: nada é gravado; a situação vem de situacao_historico()
#   e a origem (Morumbi 3D, Formulario Festas...) é sempre preservada.
# ---------------------------------------------------------------------------

def _colunas(conn, tabela: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})").fetchall()}


def _sql_pedidos_historicos(conn) -> str:
    """Pedidos abrangidos pela regra de data (inclusive os já normalizados)."""
    col_hist = "p.historico" if "historico" in _colunas(conn, "pedidos") else "0"
    return (
        f"SELECT p.id, p.cliente_id, c.nome AS cliente_nome,"
        f" p.data_evento, p.data_retirada, p.data_devolucao,"
        f" {_data_pedido_sql()} AS data_ref,"
        f" p.status_comercial, p.status_operacional, {col_hist} AS historico,"
        f" (SELECT SUM(i.quantidade * i.preco_unitario) FROM itens_pedido i"
        f"  WHERE i.pedido_id = p.id) AS valor"
        f" FROM pedidos p LEFT JOIN clientes c ON c.id = p.cliente_id"
        f" WHERE p.status_comercial != 'cancelado' AND {_t('p')}"
        f" AND {_data_pedido_sql()} < '{DATA_CORTE_FINALIZADOS}'")


def _ja_normalizado(p) -> bool:
    return (p["status_comercial"] == "finalizado"
            and p["status_operacional"] == "finalizado"
            and bool(p["historico"]))


def diagnostico_normalizacao(conn, amostra: int = 15) -> dict:
    """Relatório somente leitura: o que a normalização faria (ou fez)."""
    preparar_conexao(conn)
    historicos = [dict(r) for r in conn.execute(
        _sql_pedidos_historicos(conn) + " ORDER BY data_ref, p.id").fetchall()]
    a_alterar = [p for p in historicos if not _ja_normalizado(p)]

    total_pedidos = conn.execute(
        f"SELECT COUNT(*) FROM pedidos WHERE {_t()}").fetchone()[0]
    cancelados = conn.execute(
        f"SELECT COUNT(*) FROM pedidos WHERE status_comercial = 'cancelado' AND {_t()}"
    ).fetchone()[0]
    sem_data = conn.execute(
        f"SELECT COUNT(*) FROM pedidos p WHERE {_data_pedido_sql()} IS NULL"
        f" AND p.status_comercial != 'cancelado' AND {_t('p')}").fetchone()[0]
    col_hist = "historico" if "historico" in _colunas(conn, "pedidos") else "0"
    inconsistentes = conn.execute(
        f"SELECT COUNT(*) FROM pedidos WHERE {col_hist} = 1 AND {_t()}"
        " AND (status_comercial != 'finalizado'"
        "      OR status_operacional != 'finalizado')").fetchone()[0]

    por_origem: dict = {}
    for r in conn.execute(
            "SELECT COALESCE(origem, '(sem origem)') AS origem, status_origem,"
            " situacao_historico(origem, status_origem, data_evento) AS situacao,"
            " COUNT(*) AS n,"
            " SUM(CASE WHEN valor IS NULL OR valor = 0 THEN 1 ELSE 0 END) AS sem_valor,"
            " SUM(CASE WHEN COALESCE(data_evento, '') = '' THEN 1 ELSE 0 END) AS sem_data,"
            " MIN(NULLIF(data_evento, '')) AS primeira,"
            " MAX(NULLIF(data_evento, '')) AS ultima"
            " FROM eventos_historico GROUP BY 1, 2, 3 ORDER BY 1, 2").fetchall():
        o = por_origem.setdefault(r["origem"], {
            "total": 0, "finalizado": 0, "cancelado": 0, "pendente": 0,
            "sem_valor_finalizado": 0, "sem_data": 0, "status_origem": [],
            "oculta": r["origem"] in ORIGENS_OCULTAS})
        o["total"] += r["n"]
        o[r["situacao"]] += r["n"]
        o["sem_data"] += r["sem_data"]
        if r["situacao"] == "finalizado":
            o["sem_valor_finalizado"] += r["sem_valor"]
        o["status_origem"].append({
            "status": r["status_origem"], "situacao": r["situacao"],
            "quantidade": r["n"], "primeira": r["primeira"], "ultima": r["ultima"]})

    pendentes = [dict(r) for r in conn.execute(
        "SELECT h.id, h.origem, h.origem_id, h.data_evento, h.status_origem,"
        " c.nome AS cliente_nome FROM eventos_historico h"
        " LEFT JOIN clientes c ON c.id = h.cliente_id"
        " WHERE situacao_historico(h.origem, h.status_origem, h.data_evento) = 'pendente'"
        f" AND {_origem_visivel()}"
        " ORDER BY h.data_evento").fetchall()]

    migracao_antiga = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE tipo = 'migracao_finalizados'"
    ).fetchone()[0]

    return {
        "data_corte": DATA_CORTE_FINALIZADOS,
        "pedidos": {
            "total": total_pedidos,
            "historicos": len(historicos),
            "ja_normalizados": len(historicos) - len(a_alterar),
            "ja_finalizados": sum(1 for p in historicos
                                  if p["status_comercial"] in COMERCIAL_FATURADO),
            "ja_finalizados_sem_marca": sum(
                1 for p in a_alterar if p["status_comercial"] == "finalizado"),
            "a_alterar": len(a_alterar),
            "atuais_preservados": total_pedidos - cancelados - len(historicos),
            "cancelados": cancelados,
            "sem_data": sem_data,
            "sem_valor_historicos": sum(1 for p in historicos if not p["valor"]),
            "inconsistentes": inconsistentes,
            "amostra": a_alterar[:amostra],
            "ids_a_alterar": [p["id"] for p in a_alterar],
        },
        "historico": {
            "total": sum(o["total"] for o in por_origem.values()),
            "por_origem": por_origem,
            "pendentes": pendentes,
        },
        "migracao_antiga_executada": migracao_antiga > 0,
    }


def aplicar_normalizacao(conn, usuario_id=None) -> dict:
    """Finaliza os pedidos históricos. Idempotente: reexecutar não altera nada."""
    preparar_conexao(conn)
    agora_ = formato.agora()
    alterados = []
    with conn:
        for p in conn.execute(_sql_pedidos_historicos(conn)).fetchall():
            if _ja_normalizado(p):
                continue
            conn.execute(
                "UPDATE pedidos SET status_comercial = 'finalizado',"
                " status_operacional = 'finalizado', historico = 1,"
                " atualizado_em = ? WHERE id = ?", (agora_, p["id"]))
            alterados.append({
                "id": p["id"],
                "antes": {"status_comercial": p["status_comercial"],
                          "status_operacional": p["status_operacional"],
                          "historico": p["historico"]}})
        if alterados:
            conn.execute(
                "INSERT INTO audit_log (tenant_id, usuario_id, tipo, descricao, dados, criado_em)"
                f" VALUES ({tenant_atual()}, ?, 'normalizacao_historico', ?, ?, ?)",
                (usuario_id,
                 f"Normalizou {len(alterados)} pedidos anteriores a"
                 f" {DATA_CORTE_FINALIZADOS} como finalizados/históricos",
                 json.dumps({"data_corte": DATA_CORTE_FINALIZADOS,
                             "alterados": alterados}, ensure_ascii=False),
                 agora_))
    return {"alterados": len(alterados), "ids": [a["id"] for a in alterados]}


def verificar_operacao_sem_historicos(conn) -> dict:
    """Pós-execução: históricos não podem aparecer em nenhuma rotina operacional."""
    preparar_conexao(conn)
    if "historico" not in _colunas(conn, "pedidos"):
        return {}
    ativos = ",".join(f"'{s}'" for s in STATUS_ATIVOS)
    return {
        "na_esteira": conn.execute(
            f"SELECT COUNT(*) FROM pedidos p WHERE p.historico = 1 AND {_em_operacao()}"
        ).fetchone()[0],
        "pedidos_ativos": conn.execute(
            f"SELECT COUNT(*) FROM pedidos p WHERE p.historico = 1 AND {_em_operacao()}"
            f" AND p.status_operacional IN ({ativos})").fetchone()[0],
        "bloqueando_estoque": conn.execute(
            "SELECT COUNT(DISTINCT p.id) FROM pedidos p"
            " JOIN itens_pedido i ON i.pedido_id = p.id"
            f" WHERE p.historico = 1 AND {_em_operacao()}").fetchone()[0],
        "gerando_alerta": conn.execute(
            f"SELECT COUNT(*) FROM pedidos p WHERE p.historico = 1 AND {_em_operacao()}"
            " AND p.status_operacional NOT IN"
            " ('recolhido','conferido','finalizado','cancelado')").fetchone()[0],
    }


# ---------------------------------------------------------------------------
# Módulo Pedidos (Sprint 2): lista, detalhe, transições e linha do tempo
# ---------------------------------------------------------------------------

ROTULOS_COMERCIAL = {
    "confirmado": "Confirmado", "entregue": "Entregue", "devolvido": "Devolvido",
    "finalizado": "Finalizado", "cancelado": "Cancelado", "pendente": "Pendente",
}
ROTULOS_OPERACIONAL = {
    "preparacao": "Em preparação", "separado": "Separado", "montado": "Montado",
    "entregue": "Entregue/Retirado", "recolhido": "Recolhido/Devolvido",
    "conferido": "Conferência", "finalizado": "Finalizado", "cancelado": "Cancelado",
}
# status operacional atual -> (ação principal, explicação do momento)
PROXIMA_ACAO = {
    "preparacao": ("Marcar como separado", "O pedido está em preparação."),
    "separado": ("Registrar retirada/entrega", "Os itens já foram separados."),
    "montado": ("Registrar retirada/entrega", "O pedido está pronto para sair."),
    "entregue": ("Registrar devolução", "O pedido está com o cliente."),
    "recolhido": ("Conferir pedido", "Os itens voltaram e aguardam conferência."),
    "conferido": ("Finalizar", "Pedido conferido. Pode ser finalizado."),
}
TITULO_AVANCO = {
    "separado": "Itens separados", "montado": "Pedido montado",
    "entregue": "Retirada / entrega registrada",
    "recolhido": "Devolução registrada", "conferido": "Itens conferidos",
}
TIPOS_ITEM = ("produto", "kit", "servico")

ABAS_PEDIDOS = (
    ("todos", "Todos"), ("andamento", "Em andamento"),
    ("finalizados", "Finalizados"), ("cancelados", "Cancelados"),
    ("historico", "Histórico"),
)
# Aba pelo estado real (Sprint 2.1): importado não é sinônimo de histórico.
# Em andamento = não cancelado, não finalizado e não encerrado como histórico,
# inclusive festa importada pendente que ainda não virou pedido atual.
_CONDICAO_ABA = {
    "todos": "1 = 1",
    "andamento": ("historico = 0"
                  " AND status_comercial NOT IN ('cancelado', 'finalizado')"),
    "finalizados": "status_comercial = 'finalizado'",
    "cancelados": "status_comercial = 'cancelado'",
    "historico": "historico = 1",
}
PERIODOS_PEDIDOS = (
    ("hoje", "Hoje"), ("semana", "Esta semana"), ("este_mes", "Este mês"),
    ("mes_anterior", "Mês anterior"), ("este_ano", "Este ano"),
    ("personalizado", "Período personalizado"),
)
_ORDEM_PEDIDOS = {
    "evento": "COALESCE(data_evento, '')",
    "numero": "numero",
    "cliente": "normalizar(cliente_nome)",
    "total": "COALESCE(total, 0)",
}
POR_PAGINA_PEDIDOS = (10, 20, 50)


def _sql_base_pedidos() -> str:
    """Pedidos do sistema + importados visíveis, com a mesma forma de linha."""
    sit = "situacao_historico(h.origem, h.status_origem, h.data_evento)"
    nome_op = nome_empresa().replace("'", "''")
    return (
        "SELECT 'pedido' AS tipo, p.id AS id, p.id AS numero, p.cliente_id,"
        " c.nome AS cliente_nome, c.whatsapp AS cliente_whatsapp,"
        " c.telefone AS cliente_telefone,"
        " p.data_evento, p.data_retirada, p.data_devolucao,"
        " p.status_comercial, p.status_operacional, p.historico,"
        f" '{nome_op}' AS origem, COALESCE(p.canal, '') AS canal,"
        " COALESCE(p.fonte, '') AS fonte, p.fonte_id AS numero_origem,"
        f" {_total_pedido_sql()} AS total,"
        " (SELECT SUM(i.quantidade) FROM itens_pedido i"
        "  WHERE i.pedido_id = p.id AND i.tipo != 'servico') AS itens,"
        " (SELECT GROUP_CONCAT(i.descricao, '|') FROM itens_pedido i"
        "  WHERE i.pedido_id = p.id AND i.tipo = 'servico') AS servicos,"
        " (SELECT GROUP_CONCAT(i.descricao, ' ') FROM itens_pedido i"
        "  WHERE i.pedido_id = p.id) AS texto_itens,"
        " COALESCE(p.observacoes, '') AS observacoes, p.criado_em"
        " FROM pedidos p LEFT JOIN clientes c ON c.id = p.cliente_id"
        f" WHERE {_t('p')}"
        " UNION ALL"
        " SELECT 'historico', h.id, COALESCE(h.origem_id, h.id), h.cliente_id,"
        " c.nome, c.whatsapp, c.telefone,"
        " h.data_evento, NULL, NULL,"
        f" {sit},"
        f" CASE {sit} WHEN 'finalizado' THEN 'finalizado'"
        "  WHEN 'cancelado' THEN 'cancelado' END,"
        f" CASE {sit} WHEN 'pendente' THEN 0 ELSE 1 END,"
        f" {_operacao_sql('h.origem')}, canal_canonico(h.canal),"
        " COALESCE(h.origem, ''), h.origem_id,"
        " NULLIF(h.valor, 0), NULL, NULL, h.descricao,"
        " COALESCE(h.observacoes, ''), h.criado_em"
        " FROM eventos_historico h LEFT JOIN clientes c ON c.id = h.cliente_id"
        f" WHERE {_historico_visivel()}")


def consultar_pedidos(q: str = "", status_comercial: str = "",
                      status_operacional: str = "", inicio: str | None = None,
                      fim: str | None = None, origem: str = "",
                      aba: str = "todos", pagina: int = 1,
                      canal: str = "",
                      por_pagina: int = 10, ordem: str = "evento",
                      direcao: str = "desc") -> dict:
    """Lista paginada no banco; contagens por aba respeitam os filtros."""
    conds: list[str] = []
    params: list = []
    termo = (q or "").strip()
    if termo:
        partes = ["normalizar(cliente_nome) LIKE ?",
                  "normalizar(texto_itens) LIKE ?",
                  "normalizar(observacoes) LIKE ?"]
        like = f"%{normalizar_texto(termo)}%"
        params += [like, like, like]
        numero = termo.lstrip("#")
        if numero.isdigit():
            # também pelo número que o pedido tinha na planilha importada
            partes.append("(numero = ? OR numero_origem = ?)")
            params += [int(numero), int(numero)]
        if len(somente_digitos(termo)) >= 4:
            partes.append("digitos(COALESCE(cliente_whatsapp, '') || ' '"
                          " || COALESCE(cliente_telefone, '')) LIKE ?")
            params.append(f"%{somente_digitos(termo)}%")
        conds.append("(" + " OR ".join(partes) + ")")
    if status_comercial in set(STATUS_PEDIDO_COMERCIAL) | {"pendente"}:
        conds.append("status_comercial = ?")
        params.append(status_comercial)
    if status_operacional in STATUS_PEDIDO_OPERACIONAL:
        conds.append("status_operacional = ?")
        params.append(status_operacional)
    if inicio and fim:
        conds.append("data_evento BETWEEN ? AND ?")
        params += [inicio, fim]
    if origem:
        conds.append("origem = ?")
        params.append(origem)
    if canal:
        conds.append("canal = ?")
        params.append(canal)
    onde = " AND ".join(conds) or "1 = 1"
    aba = aba if aba in _CONDICAO_ABA else "todos"
    por_pagina = por_pagina if por_pagina in POR_PAGINA_PEDIDOS else 10
    expr = _ORDEM_PEDIDOS.get(ordem, _ORDEM_PEDIDOS["evento"])
    sentido = "ASC" if direcao == "asc" else "DESC"

    base = f"SELECT * FROM ({_sql_base_pedidos()}) WHERE {onde}"
    somas = ", ".join(f"SUM(CASE WHEN {c} THEN 1 ELSE 0 END)"
                      for c in _CONDICAO_ABA.values())
    with conectar() as conn:
        linha = conn.execute(f"SELECT {somas} FROM ({base})", params).fetchone()
        contagens = {chave: (linha[i] or 0)
                     for i, chave in enumerate(_CONDICAO_ABA)}
        total = contagens[aba]
        paginas = max(1, -(-total // por_pagina))
        pagina = min(max(1, pagina), paginas)
        rows = conn.execute(
            f"{base} AND {_CONDICAO_ABA[aba]}"
            f" ORDER BY {expr} {sentido}, numero DESC LIMIT ? OFFSET ?",
            params + [por_pagina, (pagina - 1) * por_pagina]).fetchall()

    registros = []
    for r in rows:
        d = dict(r)
        d["servicos"] = [s for s in (d["servicos"] or "").split("|") if s]
        d["fonte_rotulo"] = rotulo_fonte(d["fonte"])
        d["acoes"] = acoes_permitidas(d)
        registros.append(d)
    return {"registros": registros, "contagens": contagens, "total": total,
            "pagina": pagina, "paginas": paginas, "por_pagina": por_pagina,
            "aba": aba, "inicio_item": (pagina - 1) * por_pagina + 1 if total else 0,
            "fim_item": min(pagina * por_pagina, total)}


def acoes_permitidas(p: dict) -> set:
    """Ações de negócio válidas para o estado do pedido (perfil à parte)."""
    if p.get("tipo") == "historico":
        # importado ainda pendente: pode virar pedido atual (Sprint 2.1)
        return {"converter"} if p.get("status_comercial") == "pendente" else set()
    acoes = {"ver"}
    if p.get("historico"):
        return acoes | {"corrigir"}
    sc, so = p.get("status_comercial"), p.get("status_operacional")
    if sc in FORA_DA_OPERACAO:
        return acoes
    acoes |= {"editar", "cancelar", "ocorrencia"}
    if so in FLUXO_OPERACIONAL:
        acoes.add("avancar")
    if so == "conferido":
        acoes.add("finalizar")
    return acoes


def buscar_pedido_detalhe(id_: int) -> dict | None:
    ped = buscar_pedido_festas(id_)
    if not ped:
        return None
    with conectar() as conn:
        c = conn.execute(
            "SELECT id, nome, whatsapp, telefone, total_festas, classificacao"
            f" FROM clientes WHERE id = ? AND {_t()}", (ped["cliente_id"],)).fetchone()
    ped["cliente"] = dict(c) if c else None
    ped["origem"] = nome_empresa()
    ped["canal"] = ped.get("canal") or ""
    ped["fonte_rotulo"] = rotulo_fonte(ped.get("fonte"))
    ped["numero_origem"] = ped.get("fonte_id")
    ped["canal_original"] = ""
    if ped.get("historico_id"):
        with conectar() as conn:
            h = conn.execute("SELECT canal, status_origem, criado_em"
                             f" FROM eventos_historico WHERE id = ? AND {_t()}",
                             (ped["historico_id"],)).fetchone()
        if h:
            ped["canal_original"] = h["canal"] or ""
            ped["status_na_origem"] = h["status_origem"] or ""
            ped["importado_em"] = h["criado_em"]
    ped["tipo"] = "pedido"
    ped["produtos"] = [i for i in ped["itens"] if i["tipo"] != "servico"]
    ped["servicos"] = [i["descricao"] for i in ped["itens"] if i["tipo"] == "servico"]
    ped["qtd_itens"] = sum(i["quantidade"] for i in ped["produtos"])
    ped["acoes"] = acoes_permitidas(ped)
    ped["proxima_acao"] = (PROXIMA_ACAO.get(ped["status_operacional"])
                           if "avancar" in ped["acoes"] or "finalizar" in ped["acoes"]
                           else None)
    ped["linha_do_tempo"] = linha_do_tempo_pedido(ped)
    return ped


def buscar_historico_detalhe(id_: int) -> dict | None:
    """Registro importado em modo consulta (sem itens nem ações operacionais)."""
    with conectar() as conn:
        h = conn.execute(
            "SELECT h.*, situacao_historico(h.origem, h.status_origem, h.data_evento)"
            " AS situacao FROM eventos_historico h"
            f" WHERE h.id = ? AND {_origem_visivel()}", (id_,)).fetchone()
        if not h:
            return None
        h = dict(h)
        c = conn.execute(
            "SELECT id, nome, whatsapp, telefone, total_festas, classificacao"
            f" FROM clientes WHERE id = ? AND {_t()}", (h["cliente_id"],)).fetchone()
    h["cliente"] = dict(c) if c else None
    h["tipo"] = "historico"
    # pendente = festa ainda não encerrada: não é histórico nem modo consulta
    h["historico"] = 0 if h["situacao"] == "pendente" else 1
    h["numero"] = h["origem_id"] or h["id"]
    h["fonte"] = h["origem"]
    h["fonte_rotulo"] = rotulo_fonte(h["origem"])
    h["numero_origem"] = h["origem_id"]
    h["origem"] = operacao_da_fonte(h["origem"])
    h["canal_original"] = h["canal"] or ""
    h["canal"] = canal_canonico(h["canal"])
    h["status_na_origem"] = h["status_origem"] or ""
    h["importado_em"] = h["criado_em"]
    h["status_comercial"] = h["situacao"]
    h["status_operacional"] = "finalizado" if h["situacao"] == "finalizado" else None
    h["valor"] = h["valor"] or None
    h["acoes"] = acoes_permitidas(h)
    h["pendencias_conversao"] = motivos_sem_conversao(h)
    h["linha_do_tempo"] = [{
        "quando": h["criado_em"], "titulo": "Registro importado",
        "categoria": "Comercial",
        "detalhe": f"Fonte: {h['fonte_rotulo']} #{h['origem_id']}"
                   f" · status na origem: {h['status_origem'] or '—'}"}]
    if h["data_evento"]:
        h["linha_do_tempo"].append({
            "quando": h["data_evento"], "titulo": "Festa", "categoria": "Agenda",
            "detalhe": h["descricao"] or ""})
    h["linha_do_tempo"].sort(key=lambda e: e["quando"] or "")
    return h


def registrar_evento_pedido(conn, pedido_id: int, titulo: str, categoria: str,
                            usuario_id=None, detalhe: str = "",
                            mudancas: dict | None = None):
    conn.execute(
        "INSERT INTO audit_log (tenant_id, usuario_id, tipo, entidade, entidade_id,"
        " descricao, dados, criado_em)"
        " VALUES (?, ?, 'pedido_evento', 'pedido', ?, ?, ?, ?)",
        (tenant_atual(), usuario_id, pedido_id, f"Pedido #{pedido_id}: {titulo}",
         json.dumps({"pedido_id": pedido_id, "titulo": titulo,
                     "categoria": categoria, "detalhe": detalhe,
                     "mudancas": mudancas or {}}, ensure_ascii=False),
         formato.agora()))


_RE_PEDIDO_LEGADO = re.compile(r"[Pp]edido #(\d+)\b")


def linha_do_tempo_pedido(ped: dict) -> list:
    """Somente fatos registrados: auditoria do pedido e datas da própria agenda."""
    pid = ped["id"]
    eventos = []
    with conectar() as conn:
        rows = conn.execute(
            "SELECT a.criado_em, a.tipo, a.descricao, a.dados, u.nome AS usuario"
            " FROM audit_log a LEFT JOIN usuarios u ON u.id = a.usuario_id"
            f" WHERE {_t('a')} AND ((a.tipo = 'pedido_evento' AND a.descricao LIKE ?)"
            "    OR (a.tipo IN ('pedido', 'operacao') AND a.descricao LIKE ?))"
            " ORDER BY a.criado_em, a.id",
            (f"Pedido #{pid}: %", f"%edido #{pid}%")).fetchall()
    for r in rows:
        if r["tipo"] == "pedido_evento":
            d = json.loads(r["dados"])
            if d.get("pedido_id") != pid:
                continue
            eventos.append({"quando": r["criado_em"], "titulo": d["titulo"],
                            "categoria": d["categoria"], "detalhe": d.get("detalhe", ""),
                            "usuario": r["usuario"]})
            continue
        m = _RE_PEDIDO_LEGADO.search(r["descricao"] or "")
        if not m or int(m.group(1)) != pid:
            continue
        eventos.append({"quando": r["criado_em"], "titulo": r["descricao"],
                        "categoria": "Operação" if r["tipo"] == "operacao" else "Comercial",
                        "detalhe": "", "usuario": r["usuario"]})
    if ped.get("historico_id"):
        eventos.insert(0, {"quando": ped["criado_em"], "titulo": "Registro importado",
                           "categoria": "Comercial", "usuario": None,
                           "detalhe": f"Fonte: {rotulo_fonte(ped.get('fonte'))}"
                                      f" #{ped.get('fonte_id')}"})
    elif not any(e["titulo"].startswith("Pedido criado") for e in eventos):
        eventos.insert(0, {"quando": ped["criado_em"], "titulo": "Pedido criado",
                           "categoria": "Comercial", "detalhe": "", "usuario": None})
    hoje = _hoje_iso()
    for campo, hora, titulo in (("data_retirada", "hora_retirada", "Retirada / entrega"),
                                ("data_evento", None, "Festa"),
                                ("data_devolucao", "hora_devolucao", "Devolução")):
        data = ped.get(campo)
        if not data:
            continue
        quando = f"{data}T{ped.get(hora) or '00:00'}:00" if hora else f"{data}T00:00:00"
        eventos.append({"quando": quando, "titulo": titulo, "categoria": "Agenda",
                        "detalhe": "Previsto." if data >= hoje else "Data agendada.",
                        "usuario": None, "sem_hora": not (hora and ped.get(hora))})
    eventos.sort(key=lambda e: e["quando"] or "")
    return eventos


def finalizar_pedido(id_: int, usuario_id=None):
    with conectar() as conn:
        p = conn.execute(f"SELECT * FROM pedidos WHERE id = ? AND {_t()}",
                         (id_,)).fetchone()
        if not p:
            raise ValueError("Pedido não encontrado.")
        if "finalizar" not in acoes_permitidas(dict(p, tipo="pedido")):
            raise ValueError("Só é possível finalizar um pedido conferido.")
        conn.execute(
            "UPDATE pedidos SET status_comercial = 'finalizado',"
            " status_operacional = 'finalizado', atualizado_em = ?"
            f" WHERE id = ? AND {_t()}", (formato.agora(), id_))
        registrar_evento_pedido(
            conn, id_, "Pedido finalizado", "Comercial", usuario_id,
            mudancas={"status_comercial": [p["status_comercial"], "finalizado"],
                      "status_operacional": [p["status_operacional"], "finalizado"]})
    atualizar_classificacao_cliente(p["cliente_id"])


TIPOS_OCORRENCIA = ("Item faltando", "Item danificado", "Atraso",
                    "Cliente não compareceu", "Problema na entrega",
                    "Problema na devolução", "Outro")


def registrar_ocorrencia(id_: int, texto: str, usuario_id=None, tipo: str = ""):
    texto = (texto or "").strip()
    tipo = tipo if tipo in TIPOS_OCORRENCIA else ""
    if not texto and not tipo:
        raise ValueError("Descreva a ocorrência.")
    if len(texto) > 1000:
        raise ValueError("A ocorrência deve ter no máximo 1000 caracteres.")
    with conectar() as conn:
        if not _do_tenant(conn, "pedidos", id_):
            raise ValueError("Pedido não encontrado.")
        registrar_evento_pedido(
            conn, id_, f"Ocorrência: {tipo}" if tipo else "Ocorrência registrada",
            "Ocorrência", usuario_id, detalhe=texto)


# ---------------------------------------------------------------------------
# Sprint 2.1 — reclassificação pelo estado real do pedido
#
# Importado ≠ histórico. Uma festa importada da planilha que ainda vai
# acontecer (ou aconteceu depois do corte sem encerramento registrado) vira
# um pedido atual da Morumbi Festas: entra em "Em andamento", na Agenda e,
# no Sprint 3, na Esteira. O registro importado continua intacto em
# eventos_historico (fonte, número e canal originais) e fica ligado ao
# pedido por pedidos.historico_id (índice único: nunca dois pedidos).
# Nada é inventado: sem itens continua sem itens, sem valor continua sem valor.
# ---------------------------------------------------------------------------

# Status operacionais que mostram operação real em curso (itens com o
# cliente ou voltando): um pedido finalizado só pela data de corte com um
# desses status precisa de conferência manual.
STATUS_OPERACAO_EM_CURSO = ("entregue", "recolhido", "conferido")


def motivos_sem_conversao(h: dict) -> list:
    """Por que um importado pendente não pode virar pedido atual sozinho."""
    motivos = []
    if not h.get("data_evento"):
        motivos.append("sem data do evento")
    else:
        try:
            date.fromisoformat(h["data_evento"])
        except ValueError:
            motivos.append("data do evento inválida")
    if not h.get("cliente_id") or h.get("cliente_existe") == 0:
        motivos.append("sem cliente vinculado")
    if operacao_da_fonte(h.get("fonte") or h.get("origem")) != nome_empresa():
        motivos.append("não é da operação Morumbi Festas")
    return motivos


def _importados_pendentes(conn) -> list:
    """Importados visíveis, pendentes e ainda não promovidos, com o motivo
    de bloqueio (lista vazia = há evidência suficiente para promover)."""
    rows = conn.execute(
        "SELECT h.*, c.nome AS cliente_nome,"
        " CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS cliente_existe"
        " FROM eventos_historico h LEFT JOIN clientes c ON c.id = h.cliente_id"
        " WHERE situacao_historico(h.origem, h.status_origem, h.data_evento)"
        "       = 'pendente'"
        f" AND {_historico_visivel()}"
        " ORDER BY COALESCE(h.data_evento, '9999'), h.id").fetchall()
    pendentes = []
    for r in rows:
        d = dict(r)
        d["motivos"] = motivos_sem_conversao(d)
        pendentes.append(d)
    return pendentes


def _sql_historico_inconsistente() -> str:
    """Pedidos marcados como históricos cujo estado real não é de histórico."""
    return (
        "SELECT p.id, p.cliente_id, p.status_comercial, p.status_operacional,"
        f" {_data_pedido_sql()} AS data_ref"
        f" FROM pedidos p WHERE p.historico = 1 AND {_t('p')} AND ("
        "  p.status_comercial NOT IN ('finalizado', 'cancelado')"
        f"  OR {_data_pedido_sql()} >= '{DATA_CORTE_FINALIZADOS}')")


def _promover_importado(conn, h: dict, usuario_id, agora_: str) -> int:
    obs = " · ".join(t for t in (
        f"Pedido na planilha: {h['descricao']}" if h.get("descricao") else "",
        h.get("observacoes") or "") if t)
    cur = conn.execute(
        "INSERT INTO pedidos (tenant_id, cliente_id, data_evento, status_comercial,"
        " status_operacional, observacoes, canal, fonte, fonte_id,"
        " historico_id, valor_informado, historico, criado_em, atualizado_em)"
        " VALUES (?, ?, ?, 'confirmado', 'preparacao', ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (h["tenant_id"], h["cliente_id"], h["data_evento"], obs, canal_canonico(h.get("canal")),
         h["origem"], h["origem_id"], h["id"], h.get("valor") or None,
         h.get("criado_em") or agora_, agora_))
    pedido_id = cur.lastrowid
    registrar_evento_pedido(
        conn, pedido_id, "Reclassificado como pedido atual", "Comercial",
        usuario_id,
        detalhe=(f"Festa importada de {rotulo_fonte(h['origem'])}"
                 f" #{h['origem_id']} com evento em"
                 f" {formato.fmt_data(h['data_evento'])}: estava em modo consulta."
                 " Itens e valor não foram inventados."),
        mudancas={"historico_id": ["", h["id"]]})
    return pedido_id


def pedido_do_historico(historico_id: int) -> int | None:
    with conectar() as conn:
        r = conn.execute(f"SELECT id FROM pedidos WHERE historico_id = ? AND {_t()}",
                         (historico_id,)).fetchone()
    return r["id"] if r else None


def converter_historico_em_pedido(historico_id: int, usuario_id=None) -> int:
    """Promove um importado pendente (ação do administrador). Idempotente."""
    existente = pedido_do_historico(historico_id)
    if existente:
        return existente
    agora_ = formato.agora()
    with conectar() as conn:
        h = next((p for p in _importados_pendentes(conn)
                  if p["id"] == historico_id), None)
        if not h:
            raise ValueError("Só registros importados pendentes podem virar"
                             " pedido atual.")
        if h["motivos"]:
            raise ValueError("Não é possível converter: "
                             + ", ".join(h["motivos"]) + ".")
        pedido_id = _promover_importado(conn, h, usuario_id, agora_)
        conn.execute(
            "INSERT INTO audit_log (tenant_id, usuario_id, tipo, descricao, dados, criado_em)"
            f" VALUES ({tenant_atual()}, ?, 'reclassificacao_pedidos', ?, ?, ?)",
            (usuario_id, f"Importado {rotulo_fonte(h['origem'])}"
             f" #{h['origem_id']} convertido no pedido #{pedido_id}",
             json.dumps({"promovidos": [{"historico_id": h["id"],
                                         "pedido_id": pedido_id}]}),
             agora_))
    atualizar_classificacao_cliente(h["cliente_id"])
    return pedido_id


def _finalizados_com_operacao_em_curso(conn) -> list:
    """Pedidos que a normalização do Sprint 1 finalizou só pela data de corte
    quando já tinham itens com o cliente ou voltando: conferir à mão."""
    ids = {}
    for r in conn.execute("SELECT dados FROM audit_log"
                          f" WHERE tipo = 'normalizacao_historico' AND {_t()}"):
        try:
            alterados = json.loads(r["dados"] or "{}").get("alterados", [])
        except (TypeError, ValueError):
            continue
        for a in alterados:
            antes = a.get("antes") or {}
            if antes.get("status_operacional") in STATUS_OPERACAO_EM_CURSO:
                ids[a["id"]] = antes["status_operacional"]
    if not ids:
        return []
    marcas = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT p.id, p.data_evento, p.data_devolucao, p.status_comercial,"
        " c.nome AS cliente_nome FROM pedidos p"
        " LEFT JOIN clientes c ON c.id = p.cliente_id"
        f" WHERE p.id IN ({marcas}) AND p.status_comercial = 'finalizado'"
        f" AND {_t('p')}",
        list(ids)).fetchall()
    return [dict(r, status_antes=ids[r["id"]]) for r in rows]


def _chave_telefone(texto) -> str:
    d = somente_digitos(texto)
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    return d if len(d) >= 8 else ""


def possiveis_clientes_duplicados(conn) -> list:
    """Grupos de clientes com mesmo nome completo, telefone ou e-mail.

    Só relatório: nada é mesclado. Mesmo nome é indício, não prova.
    """
    clientes = [dict(r) for r in conn.execute(
        f"SELECT id, nome, whatsapp, telefone, email, cpf_cnpj FROM clientes"
        f" WHERE {_t()}")]
    pai = {c["id"]: c["id"] for c in clientes}

    def raiz(i):
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    motivos: dict = {}
    indices: dict = {}
    for c in clientes:
        nome = " ".join(normalizar_texto(c["nome"]).split())
        chaves = []
        if len(nome.split()) >= 2:
            chaves.append(("mesmo nome", nome))
        for campo in ("whatsapp", "telefone"):
            fone = _chave_telefone(c.get(campo))
            if fone:
                chaves.append(("mesmo telefone", fone))
        email = (c.get("email") or "").strip().lower()
        if email:
            chaves.append(("mesmo e-mail", email))
        doc = somente_digitos(c.get("cpf_cnpj"))
        if len(doc) >= 11:
            chaves.append(("mesmo CPF/CNPJ", doc))
        for chave in set(chaves):
            if chave in indices:
                a, b = raiz(indices[chave]), raiz(c["id"])
                if a != b:
                    pai[b] = a
                motivos.setdefault(chave, {indices[chave]}).add(c["id"])
            else:
                indices[chave] = c["id"]

    grupos: dict = {}
    for c in clientes:
        grupos.setdefault(raiz(c["id"]), []).append(c)
    resultado = []
    for membros in grupos.values():
        if len(membros) < 2:
            continue
        ids = {m["id"] for m in membros}
        razoes = sorted({m for (m, _), quem in motivos.items() if quem & ids})
        resultado.append({"clientes": sorted(membros, key=lambda m: m["id"]),
                          "motivos": razoes})
    return sorted(resultado, key=lambda g: g["clientes"][0]["id"])


def _vinculos_por_nome_ambiguos(conn) -> list:
    """Importados ligados a um cliente cujo nome é compartilhado por outro:
    o importador pode ter escolhido o cliente errado (vínculo por nome)."""
    nomes: dict = {}
    for c in conn.execute(f"SELECT id, nome FROM clientes WHERE {_t()}"):
        nomes.setdefault(" ".join(normalizar_texto(c["nome"]).split()),
                         []).append(c["id"])
    repetidos = {i for ids in nomes.values() if len(ids) > 1 for i in ids}
    if not repetidos:
        return []
    marcas = ",".join("?" * len(repetidos))
    return [dict(r) for r in conn.execute(
        "SELECT h.id, h.origem, h.origem_id, h.data_evento, h.cliente_id,"
        " c.nome AS cliente_nome FROM eventos_historico h"
        " JOIN clientes c ON c.id = h.cliente_id"
        f" WHERE h.cliente_id IN ({marcas}) AND {_origem_visivel()}"
        " ORDER BY h.data_evento", list(repetidos)).fetchall()]


def diagnostico_reclassificacao(conn) -> dict:
    """Relatório somente leitura do Sprint 2.1 (antes e depois da rotina)."""
    preparar_conexao(conn)
    hoje = _hoje_iso()
    um = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731

    peds = f"pedidos p WHERE {_t('p')}"  # só a empresa atual
    ped = {
        "total": um(f"SELECT COUNT(*) FROM {peds}"),
        "promovidos": um(f"SELECT COUNT(*) FROM {peds}"
                         " AND historico_id IS NOT NULL"),
        "historicos": um(f"SELECT COUNT(*) FROM {peds} AND historico = 1"),
        "finalizados": um(f"SELECT COUNT(*) FROM {peds}"
                          " AND status_comercial = 'finalizado'"),
        "cancelados": um(f"SELECT COUNT(*) FROM {peds}"
                         " AND status_comercial = 'cancelado'"),
        "em_andamento": um(f"SELECT COUNT(*) FROM {peds} AND historico = 0"
                           " AND status_comercial NOT IN"
                           " ('finalizado', 'cancelado')"),
        "futuros": um(f"SELECT COUNT(*) FROM {peds}"
                      " AND status_comercial != 'cancelado'"
                      " AND data_evento >= ?", hoje),
        "passados_com_operacao_pendente": um(
            f"SELECT COUNT(*) FROM {peds} AND p.historico = 0"
            " AND p.status_comercial NOT IN ('finalizado', 'cancelado')"
            f" AND {_data_pedido_sql()} < ?", hoje),
        "sem_itens_em_andamento": um(
            f"SELECT COUNT(*) FROM {peds} AND p.historico = 0"
            " AND p.status_comercial NOT IN ('finalizado', 'cancelado')"
            " AND NOT EXISTS (SELECT 1 FROM itens_pedido i"
            "                 WHERE i.pedido_id = p.id)"),
        "historico_inconsistente": [dict(r) for r in conn.execute(
            _sql_historico_inconsistente())],
    }

    sit = "situacao_historico(h.origem, h.status_origem, h.data_evento)"
    imp = {"por_situacao": {"finalizado": 0, "cancelado": 0, "pendente": 0}}
    for r in conn.execute(f"SELECT {sit} AS s, COUNT(*) FROM eventos_historico h"
                          f" WHERE {_historico_visivel()} GROUP BY 1"):
        imp["por_situacao"][r[0]] = r[1]
    imp["visiveis"] = sum(imp["por_situacao"].values())
    imp["ocultos"] = um("SELECT COUNT(*) FROM eventos_historico h"
                        f" WHERE {_t('h')} AND NOT ({_origem_visivel()})")
    imp["formulario_festas"] = um("SELECT COUNT(*) FROM eventos_historico"
                                  f" WHERE origem = 'Formulario Festas' AND {_t()}")
    imp["futuros"] = um(f"SELECT COUNT(*) FROM eventos_historico h"
                        f" WHERE {_historico_visivel()} AND {sit} = 'pendente'"
                        " AND h.data_evento >= ?", hoje)
    pendentes = _importados_pendentes(conn)
    imp["a_promover"] = [p for p in pendentes if not p["motivos"]]
    imp["sem_evidencia"] = [p for p in pendentes if p["motivos"]]
    imp["finalizados_data_futura"] = [dict(r) for r in conn.execute(
        "SELECT h.id, h.origem, h.origem_id, h.data_evento, c.nome AS cliente_nome"
        " FROM eventos_historico h LEFT JOIN clientes c ON c.id = h.cliente_id"
        f" WHERE {_historico_visivel()} AND {sit} = 'finalizado'"
        " AND h.data_evento > ? ORDER BY h.data_evento", (hoje,))]

    canais: dict = {}
    for r in conn.execute(
            "SELECT canal_canonico(canal) AS c, COUNT(*) FROM eventos_historico"
            f" WHERE {_origem_visivel('')} GROUP BY 1"):
        canais[r[0] or "(não informado)"] = r[1]
    for r in conn.execute("SELECT COALESCE(NULLIF(canal, ''), '(não informado)'),"
                          " COUNT(*) FROM pedidos WHERE historico_id IS NULL"
                          f" AND {_t()} GROUP BY 1"):
        canais[r[0]] = canais.get(r[0], 0) + r[1]

    duplicados = possiveis_clientes_duplicados(conn)
    ambiguos = _vinculos_por_nome_ambiguos(conn)
    em_curso = _finalizados_com_operacao_em_curso(conn)
    # registros distintos (um importado pode ter mais de um motivo)
    manual = len({("h", h["id"]) for h in (imp["sem_evidencia"]
                                            + imp["finalizados_data_futura"]
                                            + ambiguos)}
                 | {("p", p["id"]) for p in em_curso})

    return {
        "hoje": hoje,
        "data_corte": DATA_CORTE_FINALIZADOS,
        "pedidos": ped,
        "importados": imp,
        "canais": canais,
        "clientes_duplicados": duplicados,
        "vinculos_ambiguos": ambiguos,
        "finalizados_com_operacao_em_curso": em_curso,
        "resumo": {
            "analisados": ped["total"] + imp["visiveis"],
            "historicos": ped["historicos"] + imp["por_situacao"]["finalizado"],
            "futuros": ped["futuros"] + imp["futuros"],
            "a_reclassificar": (len(imp["a_promover"])
                                + len(ped["historico_inconsistente"])),
            "reclassificados": ped["promovidos"],
            "cancelados": ped["cancelados"] + imp["por_situacao"]["cancelado"],
            "finalizados": ped["finalizados"] + imp["por_situacao"]["finalizado"],
            "formulario_festas": imp["formulario_festas"],
            "canal_identificado": sum(n for c, n in canais.items()
                                      if c != "(não informado)"),
            "clientes_duplicados": sum(len(g["clientes"]) for g in duplicados),
            "grupos_duplicados": len(duplicados),
            "analise_manual": manual,
        },
    }


def aplicar_reclassificacao(conn, usuario_id=None) -> dict:
    """Promove importados pendentes com evidência e corrige marcas de histórico
    inconsistentes. Idempotente: a segunda execução não altera nada."""
    preparar_conexao(conn)
    agora_ = formato.agora()
    promovidos, desmarcados, clientes = [], [], set()
    with conn:
        for h in _importados_pendentes(conn):
            if h["motivos"]:
                continue
            pid = _promover_importado(conn, h, usuario_id, agora_)
            promovidos.append({"historico_id": h["id"], "pedido_id": pid,
                               "fonte": h["origem"], "numero_origem": h["origem_id"]})
            clientes.add(h["cliente_id"])
        for p in conn.execute(_sql_historico_inconsistente()).fetchall():
            conn.execute("UPDATE pedidos SET historico = 0, atualizado_em = ?"
                         " WHERE id = ?", (agora_, p["id"]))
            motivo = ("status " + p["status_comercial"]
                      if p["status_comercial"] not in FORA_DA_OPERACAO
                      else f"data {formato.fmt_data(p['data_ref'])}")
            registrar_evento_pedido(
                conn, p["id"], "Marcação de histórico corrigida", "Comercial",
                usuario_id, detalhe=f"Não é histórico ({motivo}).",
                mudancas={"historico": [1, 0]})
            desmarcados.append(p["id"])
            clientes.add(p["cliente_id"])
        if promovidos or desmarcados:
            conn.execute(
                "INSERT INTO audit_log (tenant_id, usuario_id, tipo, descricao, dados, criado_em)"
                f" VALUES ({tenant_atual()}, ?, 'reclassificacao_pedidos', ?, ?, ?)",
                (usuario_id,
                 f"Reclassificação: {len(promovidos)} importados viraram pedidos"
                 f" atuais; {len(desmarcados)} marcações de histórico corrigidas",
                 json.dumps({"promovidos": promovidos, "desmarcados": desmarcados},
                            ensure_ascii=False),
                 agora_))
    for cid in clientes:
        atualizar_classificacao_cliente(cid)
    return {"promovidos": promovidos, "desmarcados": desmarcados}


# ---------------------------------------------------------------------------
# Sprint 3 — Esteira de pedidos
#
# A esteira é uma visão operacional dos próprios pedidos: não há tabela nem
# status paralelos. Entram só pedidos atuais da empresa (nunca históricos
# nem cancelados); os finalizados aparecem por alguns dias na última coluna.
# Atraso, indicadores e agrupamento são calculados aqui e usados pela
# esteira e pelo Dashboard.
# ---------------------------------------------------------------------------

ETAPAS_OPERACAO = (
    ("preparacao", "Preparação", ("preparacao",)),
    ("separado", "Separado", ("separado", "montado")),
    ("entregue", "Entregue / Retirado", ("entregue",)),
    ("recolhido", "Recolhido / Devolvido", ("recolhido",)),
    ("conferencia", "Conferência", ("conferido",)),
    ("finalizado", "Finalizado", ("finalizado",)),
)
COLUNA_DO_STATUS = {s: chave for chave, _, sts in ETAPAS_OPERACAO for s in sts}
ACAO_DA_ETAPA = {
    "preparacao": "Marcar como separado", "separado": "Registrar retirada/entrega",
    "montado": "Registrar retirada/entrega", "entregue": "Registrar devolução",
    "recolhido": "Conferir pedido", "conferido": "Finalizar",
}
DIAS_FINALIZADOS_NA_ESTEIRA = 7
FILTROS_EVENTO = (("", "Todos os eventos"), ("hoje", "Festa hoje"),
                  ("amanha", "Festa amanhã"), ("proximos", "Próximos eventos"))


def destino_da_etapa(status: str) -> str | None:
    return "finalizado" if status == "conferido" else FLUXO_OPERACIONAL.get(status)


def prazo_da_etapa(p: dict) -> str | None:
    """Data até a qual a ação da etapa atual deveria acontecer."""
    so = p.get("status_operacional")
    if so in ("preparacao", "separado", "montado"):
        return p.get("data_retirada") or p.get("data_evento")
    if so in ("entregue", "recolhido", "conferido"):
        return p.get("data_devolucao") or p.get("data_evento")
    return None


def situacao_prazo(p: dict, dia: str) -> str:
    """'atrasado', 'no_prazo' ou 'concluido' — regra única (esteira e Dashboard).

    Atrasado: a ação da etapa atual tinha data anterior ao dia de referência
    e não foi registrada (saída não feita, devolução não registrada,
    conferência pendente após a devolução…).
    """
    if p.get("status_operacional") == "finalizado" or p.get("status_comercial") == "finalizado":
        return "concluido"
    limite = prazo_da_etapa(p)
    return "atrasado" if limite and limite < dia else "no_prazo"


def _rotulo_proxima(p: dict, situacao: str, dia: str) -> str:
    so = p["status_operacional"]
    if so == "preparacao":
        return "Prioridade" if situacao == "atrasado" else "Preparar"
    if so in ("separado", "montado"):
        return "Pronto"
    if so == "entregue":
        return "Em evento" if (p.get("data_devolucao") or dia) >= dia else "Devolver"
    return {"recolhido": "Conferir", "conferido": "Conferir itens",
            "finalizado": "Concluído"}.get(so, "")


def _pedidos_da_esteira(conn, dia: str) -> list:
    """Pedidos atuais em operação + finalizados nos últimos dias (por empresa)."""
    inicio_fin = (date.fromisoformat(dia)
                  - timedelta(days=DIAS_FINALIZADOS_NA_ESTEIRA - 1)).isoformat()
    finalizado_em = (
        "(SELECT MAX(a.criado_em) FROM audit_log a WHERE a.tipo = 'pedido_evento'"
        " AND a.entidade = 'pedido' AND a.entidade_id = p.id"
        " AND a.descricao LIKE '%: Pedido finalizado')")
    rows = conn.execute(
        "SELECT p.id, p.cliente_id, p.data_evento, p.data_retirada, p.data_devolucao,"
        " p.hora_retirada, p.status_comercial, p.status_operacional,"
        f" {_SQL_NOME_RESPONSAVEL} AS responsavel, p.atualizado_em,"
        " c.nome AS cliente_nome, c.whatsapp AS cliente_whatsapp,"
        " c.telefone AS cliente_telefone,"
        f" {finalizado_em} AS finalizado_em"
        " FROM pedidos p LEFT JOIN clientes c ON c.id = p.cliente_id"
        " LEFT JOIN usuarios u ON u.id = p.responsavel_id"
        f" WHERE {_t('p')} AND p.historico = 0 AND p.status_comercial != 'cancelado'"
        " AND (p.status_operacional NOT IN ('finalizado', 'cancelado')"
        "      AND p.status_comercial != 'finalizado'"
        "   OR p.status_operacional = 'finalizado')").fetchall()
    pedidos = []
    for r in rows:
        p = dict(r)
        if p["status_operacional"] == "finalizado":
            quando = (p["finalizado_em"] or p["atualizado_em"] or "")[:10]
            if not (inicio_fin <= quando <= dia):
                continue
        pedidos.append(p)
    _resumir_itens(conn, pedidos)
    return pedidos


def _resumir_itens(conn, pedidos: list) -> None:
    """Quantidade, item principal e serviços de cada pedido (uma consulta só)."""
    if not pedidos:
        return
    ids = [p["id"] for p in pedidos]
    marcas = ",".join("?" * len(ids))
    itens: dict = {}
    for i in conn.execute(f"SELECT pedido_id, tipo, item_id, descricao, quantidade,"
                          f" preco_unitario FROM itens_pedido WHERE pedido_id IN ({marcas})"
                          " ORDER BY id", ids):
        itens.setdefault(i["pedido_id"], []).append(dict(i))
    for p in pedidos:
        lista = itens.get(p["id"], [])
        fisicos = [i for i in lista if i["tipo"] != "servico"]
        p["itens"] = sum(i["quantidade"] for i in fisicos)
        # item principal: o kit (ou produto) de maior valor no pedido
        principal = max(fisicos, key=lambda i: (i["tipo"] == "kit",
                                                i["quantidade"] * i["preco_unitario"]),
                        default=None)
        p["principal"] = principal["descricao"] if principal else ""
        p["servicos"] = [i["descricao"] for i in lista if i["tipo"] == "servico"]
        p["lista_itens"] = lista


def _passa_filtros(p: dict, dia: str, f: dict) -> bool:
    amanha = (date.fromisoformat(dia) + timedelta(days=1)).isoformat()
    ev = p.get("data_evento") or ""
    if f.get("evento") == "hoje" and ev != dia:
        return False
    if f.get("evento") == "amanha" and ev != amanha:
        return False
    if f.get("evento") == "proximos" and not ev > amanha:
        return False
    if f.get("servico") and normalizar_texto(f["servico"]) not in {
            " ".join(normalizar_texto(s).split()) for s in p["servicos"]}:
        return False
    if f.get("responsavel") and normalizar_texto(f["responsavel"]) != " ".join(
            normalizar_texto(p["responsavel"]).split()):
        return False
    if f.get("status") and COLUNA_DO_STATUS.get(p["status_operacional"]) != f["status"]:
        return False
    termo = (f.get("q") or "").strip()
    if termo:
        numero = termo.lstrip("#")
        digitos = somente_digitos(termo)
        achou = (numero.isdigit() and int(numero) == p["id"]) \
            or normalizar_texto(termo) in normalizar_texto(p["cliente_nome"]) \
            or (len(digitos) >= 4 and digitos in somente_digitos(
                f"{p['cliente_whatsapp'] or ''} {p['cliente_telefone'] or ''}"))
        if not achou:
            return False
    return True


def indicadores_esteira(dia: str | None = None, pedidos: list | None = None) -> dict:
    """KPIs da operação no dia (não dependem dos filtros da tela)."""
    dia = dia or _hoje_iso()
    with conectar() as conn:
        if pedidos is None:
            pedidos = _pedidos_da_esteira(conn, dia)
        entregas = conn.execute(
            "SELECT COUNT(*) FROM pedidos WHERE data_retirada = ? AND historico = 0"
            f" AND status_comercial != 'cancelado' AND {_t()}", (dia,)).fetchone()[0]
    ativos = [p for p in pedidos if p["status_operacional"] != "finalizado"]
    atrasados = sum(1 for p in ativos if situacao_prazo(p, dia) == "atrasado")
    return {"na_esteira": len(ativos), "atrasados": atrasados,
            "no_prazo": len(ativos) - atrasados, "entregas_dia": entregas}


def quadro_esteira(dia: str | None = None, filtros: dict | None = None) -> dict:
    """Colunas da esteira com os cartões já prontos para a tela."""
    dia = dia or _hoje_iso()
    filtros = filtros or {}
    with conectar() as conn:
        pedidos = _pedidos_da_esteira(conn, dia)
    kpis = indicadores_esteira(dia, pedidos)
    colunas = {chave: {"chave": chave, "rotulo": rotulo, "cartoes": []}
               for chave, rotulo, _ in ETAPAS_OPERACAO}
    for p in pedidos:
        if not _passa_filtros(p, dia, filtros):
            continue
        situacao = situacao_prazo(p, dia)
        destino = destino_da_etapa(p["status_operacional"])
        cartao = dict(p, situacao=situacao,
                      proxima=_rotulo_proxima(p, situacao, dia),
                      acao=ACAO_DA_ETAPA.get(p["status_operacional"]),
                      destino=destino,
                      coluna_destino=COLUNA_DO_STATUS.get(destino) if destino else None)
        colunas[COLUNA_DO_STATUS.get(p["status_operacional"], "preparacao")][
            "cartoes"].append(cartao)
    for col in colunas.values():
        # atrasados primeiro, depois pela data da próxima ação
        col["cartoes"].sort(key=lambda c: (c["situacao"] != "atrasado",
                                           prazo_da_etapa(c) or "9999", c["id"]))
        if col["chave"] == "finalizado":
            col["cartoes"].sort(key=lambda c: c["finalizado_em"] or "", reverse=True)
    return {"dia": dia, "kpis": kpis, "colunas": list(colunas.values()),
            **_opcoes_de_filtro(pedidos)}


def _opcoes_de_filtro(pedidos: list) -> dict:
    """Responsáveis (usuários + nomes nos pedidos) e serviços para os filtros."""
    responsaveis = {normalizar_texto(u["nome"]): u["nome"] for u in listar_usuarios()}
    for p in pedidos:
        if (p.get("responsavel") or "").strip():
            responsaveis.setdefault(normalizar_texto(p["responsavel"]).strip(),
                                    p["responsavel"].strip())
    servicos = {normalizar_texto(s["nome"]): s["nome"] for s in listar_servicos()}
    for p in pedidos:
        for s in p.get("servicos") or []:
            servicos.setdefault(" ".join(normalizar_texto(s).split()), " ".join(s.split()))
    return {"responsaveis": sorted(responsaveis.values(), key=normalizar_texto),
            "servicos": sorted(servicos.values(), key=normalizar_texto)}


# ---------------------------------------------------------------------------
# Sprint 4 — Agenda: projeção temporal dos pedidos (nada é gravado)
#
# Cada pedido gera, a partir das próprias datas, até três compromissos:
# a festa (data_evento), a saída (data_retirada: "Entrega" quando o pedido tem
# serviço de entrega/montagem, senão "Retirada" pelo cliente) e a devolução
# (data_devolucao). Pedidos e registros históricos aparecem só pela data da
# festa, para consulta: não geram tarefa, atraso nem conflito. Cancelados só
# aparecem quando o filtro de cancelados é escolhido.
# ---------------------------------------------------------------------------

TIPOS_AGENDA = (("evento", "Evento"), ("retirada", "Retirada"), ("entrega", "Entrega"),
                ("devolucao", "Devolução"), ("historico", "Histórico"))
ROTULO_TIPO_AGENDA = dict(TIPOS_AGENDA)
STATUS_AGENDA = tuple((chave, rotulo) for chave, rotulo, _ in ETAPAS_OPERACAO) + (
    ("atrasado", "Em atraso"), ("conflito", "Conflito/atenção"),
    ("cancelado", "Cancelados"))
# Serviços que indicam saída feita pela equipe; sem eles, o cliente retira.
_TRECHOS_ENTREGA = ("entreg", "montag")
# Dois compromissos do mesmo responsável a menos disto (minutos) se chocam.
MINUTOS_CHOQUE_RESPONSAVEL = 30
# O compromisso ainda está por fazer enquanto o pedido está nestas etapas.
_PENDENTE_NAS_ETAPAS = {
    "saida": ("preparacao", "separado", "montado"),
    "devolucao": ("preparacao", "separado", "montado", "entregue"),
}


def _tipo_da_saida(servicos: list) -> str:
    nomes = " ".join(normalizar_texto(s) for s in servicos)
    return "entrega" if any(t in nomes for t in _TRECHOS_ENTREGA) else "retirada"


def _minutos(hora: str) -> int | None:
    if not hora or not re.fullmatch(r"\d{2}:\d{2}", hora):
        return None
    return int(hora[:2]) * 60 + int(hora[3:])


def _alertas_das_datas(p: dict) -> list:
    """Datas faltando ou incoerentes num pedido em operação: (nível, motivo)."""
    alertas = []
    ev, ret, dev = p.get("data_evento"), p.get("data_retirada"), p.get("data_devolucao")
    for valor, texto in ((ev, "Pedido sem data do evento."),
                         (ret, "Pedido sem data de retirada/entrega."),
                         (dev, "Pedido sem data de devolução.")):
        if not valor:
            alertas.append(("atencao", texto))
    if ret and dev and ret > dev:
        alertas.append(("conflito", "Devolução marcada antes da retirada/entrega."))
    if ev and dev and dev < ev:
        alertas.append(("conflito", "Devolução marcada antes do evento."))
    if ev and ret and ret > ev:
        alertas.append(("conflito", "Retirada/entrega marcada depois do evento."))
    if ret and ret == dev:
        saida, volta = _minutos(p.get("hora_retirada")), _minutos(p.get("hora_devolucao"))
        if saida is not None and volta is not None and volta <= saida:
            alertas.append(("conflito", "Devolução no mesmo dia, antes do horário da saída."))
    return alertas


def _pedidos_da_agenda(conn, inicio: str, fim: str) -> list:
    """Pedidos com alguma data no período (cada data por índice próprio)."""
    rows = conn.execute(
        "SELECT p.id, p.cliente_id, p.data_evento, p.data_retirada, p.data_devolucao,"
        " p.hora_retirada, p.hora_devolucao, p.local_evento, p.status_comercial,"
        " p.status_operacional, p.historico, p.responsavel_id,"
        f" {_SQL_NOME_RESPONSAVEL} AS responsavel,"
        " c.nome AS cliente_nome, c.whatsapp AS cliente_whatsapp,"
        " c.telefone AS cliente_telefone"
        " FROM pedidos p LEFT JOIN clientes c ON c.id = p.cliente_id"
        " LEFT JOIN usuarios u ON u.id = p.responsavel_id"
        f" WHERE {_t('p')} AND p.id IN ("
        f" SELECT id FROM pedidos WHERE {_t()} AND data_evento BETWEEN ? AND ?"
        f" UNION SELECT id FROM pedidos WHERE {_t()} AND data_retirada BETWEEN ? AND ?"
        f" UNION SELECT id FROM pedidos WHERE {_t()} AND data_devolucao BETWEEN ? AND ?)",
        (inicio, fim) * 3).fetchall()
    pedidos = [dict(r) for r in rows]
    _resumir_itens(conn, pedidos)
    return pedidos


def _historicos_da_agenda(conn, inicio: str, fim: str) -> list:
    """Festas importadas (planilha) que não viraram pedido, no período."""
    return [dict(r) for r in conn.execute(
        "SELECT h.id, h.cliente_id, h.data_evento, h.descricao, h.status_origem,"
        " situacao_historico(h.origem, h.status_origem, h.data_evento) AS situacao,"
        " c.nome AS cliente_nome, c.whatsapp AS cliente_whatsapp,"
        " c.telefone AS cliente_telefone"
        " FROM eventos_historico h LEFT JOIN clientes c ON c.id = h.cliente_id"
        f" WHERE h.data_evento BETWEEN ? AND ? AND {_historico_visivel()}",
        (inicio, fim)).fetchall()]


def _compromissos_do_pedido(p: dict, inicio: str, fim: str, agora_: str) -> list:
    sc, so = p["status_comercial"], p["status_operacional"]
    cancelado = sc == "cancelado" or so == "cancelado"
    base = {
        "pedido_id": p["id"], "historico_id": None, "cliente_id": p["cliente_id"],
        "cliente_nome": p.get("cliente_nome") or "",
        "cliente_contato": f"{p.get('cliente_whatsapp') or ''} {p.get('cliente_telefone') or ''}",
        "principal": p.get("principal") or "", "itens": p.get("itens") or 0,
        "servicos": p.get("servicos") or [],
        "descricoes": [i["descricao"] for i in p.get("lista_itens") or []],
        "responsavel": (p.get("responsavel") or "").strip(),
        "responsavel_chave": (f"u{p['responsavel_id']}" if p.get("responsavel_id")
                              else " ".join(normalizar_texto(p.get("responsavel")).split())),
        "local": p.get("local_evento") or "",
        "data_evento": p.get("data_evento"), "data_retirada": p.get("data_retirada"),
        "data_devolucao": p.get("data_devolucao"),
        "hora_retirada": p.get("hora_retirada") or "",
        "hora_devolucao": p.get("hora_devolucao") or "",
        "status_comercial": sc, "status_operacional": so,
        "etapa": None if cancelado or p["historico"] else COLUNA_DO_STATUS.get(so),
        "cancelado": cancelado, "historico": bool(p["historico"]),
        "operacional": not (cancelado or p["historico"] or sc in FORA_DA_OPERACAO
                            or so == "finalizado"),
        "atrasado": False, "alertas": [],
    }
    if cancelado:
        base["situacao"], base["rotulo_status"] = "cancelado", "Cancelado"
    elif p["historico"]:
        base["situacao"], base["rotulo_status"] = "historico", "Histórico"
    elif so == "finalizado" or sc == "finalizado":
        base["situacao"], base["rotulo_status"] = "finalizado", "Finalizado"
    else:
        base["situacao"] = "andamento"
        base["rotulo_status"] = ROTULOS_OPERACIONAL.get(so, so)

    if p["historico"]:
        datas = [("historico", p.get("data_evento"), "")]
    else:
        datas = [("evento", p.get("data_evento"), ""),
                 (_tipo_da_saida(base["servicos"]), p.get("data_retirada"),
                  base["hora_retirada"]),
                 ("devolucao", p.get("data_devolucao"), base["hora_devolucao"])]
    alertas = _alertas_das_datas(p) if base["operacional"] else []
    lista = []
    for tipo, data, hora in datas:
        if not data or not (inicio <= data <= fim):
            continue
        c = dict(base, chave=f"p{p['id']}-{tipo}", tipo=tipo,
                 rotulo=ROTULO_TIPO_AGENDA[tipo], data=data, hora=hora,
                 alertas=list(alertas))
        if base["operacional"]:
            # a festa só vale como prazo quando o pedido não tem data de saída
            grupo = ("devolucao" if tipo == "devolucao" else
                     "saida" if tipo != "evento" or not p.get("data_retirada") else None)
            limite = f"{data}T{hora or '23:59'}:59" if hora else f"{data}T23:59:59"
            c["atrasado"] = bool(grupo and so in _PENDENTE_NAS_ETAPAS[grupo]
                                 and limite < agora_)
        lista.append(c)
    return lista


def _compromisso_importado(h: dict) -> dict:
    situacao = h["situacao"]
    rotulo = {"finalizado": "Histórico importado", "cancelado": "Cancelado na origem",
              "pendente": "Importado — converter em pedido"}.get(situacao, "Histórico importado")
    return {
        "chave": f"h{h['id']}", "tipo": "historico", "rotulo": "Histórico",
        "data": h["data_evento"], "hora": "", "pedido_id": None, "historico_id": h["id"],
        "cliente_id": h["cliente_id"], "cliente_nome": h.get("cliente_nome") or "",
        "cliente_contato": f"{h.get('cliente_whatsapp') or ''} {h.get('cliente_telefone') or ''}",
        "principal": (h.get("descricao") or "").strip(), "itens": 0, "servicos": [],
        "descricoes": [h.get("descricao") or ""], "responsavel": "", "responsavel_chave": "",
        "local": "", "data_evento": h["data_evento"], "data_retirada": None,
        "data_devolucao": None, "hora_retirada": "", "hora_devolucao": "",
        "status_comercial": situacao, "status_operacional": None, "etapa": None,
        "cancelado": situacao == "cancelado", "historico": True, "operacional": False,
        "atrasado": False, "alertas": [], "importado": True,
        "situacao": "cancelado" if situacao == "cancelado" else "historico",
        "rotulo_status": rotulo,
    }


def _marcar_choques_de_responsavel(compromissos: list) -> None:
    """Mesmo responsável com dois compromissos com horário muito próximo."""
    grupos: dict = {}
    for c in compromissos:
        if (c["operacional"] and c["tipo"] != "evento" and c["responsavel_chave"]
                and _minutos(c["hora"]) is not None):
            grupos.setdefault((c["data"], c["responsavel_chave"]), []).append(c)
    for lista in grupos.values():
        lista.sort(key=lambda c: c["hora"])
        for a, b in zip(lista, lista[1:]):
            if (b["pedido_id"] != a["pedido_id"] and
                    _minutos(b["hora"]) - _minutos(a["hora"]) < MINUTOS_CHOQUE_RESPONSAVEL):
                for c, outro in ((a, b), (b, a)):
                    c["alertas"].append((
                        "atencao", f"{c['responsavel']} também tem {outro['rotulo'].lower()}"
                                   f" do pedido #{outro['pedido_id']} às {outro['hora']}."))


def _marcar_faltas_de_estoque(conn, pedidos: list, compromissos: list) -> None:
    """Produto reservado além do estoque no período do pedido (conflito)."""
    por_pedido: dict = {}
    for p in pedidos:
        if (p["historico"] or p["status_comercial"] in FORA_DA_OPERACAO
                or not p.get("data_retirada") or not p.get("data_devolucao")
                or p["data_retirada"] > p["data_devolucao"]):
            continue
        faltas = _faltas_de_estoque(conn, p.get("lista_itens") or [],
                                    p["data_retirada"], p["data_devolucao"], p["id"])
        if faltas:
            por_pedido[p["id"]] = [("conflito", f.replace("esta em manutencao",
                                                          "está em manutenção")
                                                 .replace("Disponivel", "Disponível"))
                                   for f in faltas]
    for c in compromissos:
        c["alertas"].extend(por_pedido.get(c["pedido_id"], []))


def _nivel_alerta(c: dict) -> str | None:
    niveis = {n for n, _ in c["alertas"]}
    return "conflito" if "conflito" in niveis else ("atencao" if niveis else None)


def _ordem_no_dia(c: dict) -> tuple:
    # festa e históricos primeiro (o dia todo), depois por horário; sem horário no fim
    dia_todo = c["tipo"] in ("evento", "historico")
    return (not dia_todo, c["hora"] == "", c["hora"], c["pedido_id"] or 0, c["chave"])


def _passa_filtros_agenda(c: dict, f: dict) -> bool:
    status = f.get("status") or ""
    if c["cancelado"] != (status == "cancelado"):
        return False
    tipo = f.get("tipo") or ""
    if tipo and c["tipo"] != tipo:
        return False
    if f.get("servico") and normalizar_texto(f["servico"]) not in {
            " ".join(normalizar_texto(s).split()) for s in c["servicos"]}:
        return False
    if f.get("responsavel") and normalizar_texto(f["responsavel"]) != " ".join(
            normalizar_texto(c["responsavel"]).split()):
        return False
    if status == "atrasado" and not c["atrasado"]:
        return False
    if status == "conflito" and not c["alertas"]:
        return False
    if status in COLUNA_DO_STATUS.values() and c["etapa"] != status:
        return False
    termo = (f.get("q") or "").strip()
    if termo:
        numero = termo.lstrip("#")
        digitos = somente_digitos(termo)
        chave = normalizar_texto(termo)
        achou = (numero.isdigit() and int(numero) == c["pedido_id"]) \
            or chave in normalizar_texto(c["cliente_nome"]) \
            or any(chave in normalizar_texto(d) for d in c["descricoes"]) \
            or (len(digitos) >= 4 and digitos in somente_digitos(c["cliente_contato"]))
        if not achou:
            return False
    return True


def filtros_agenda(args) -> dict:
    """Filtros aceitos pela Agenda; valores fora das listas são descartados."""
    tipo = args.get("tipo", "")
    status = args.get("status", "")
    return {
        "tipo": tipo if tipo in ROTULO_TIPO_AGENDA else "",
        "servico": (args.get("servico") or "").strip()[:80],
        "responsavel": (args.get("responsavel") or "").strip()[:200],
        "status": status if status in dict(STATUS_AGENDA) else "",
        "q": (args.get("q") or "").strip()[:100],
    }


def agenda_periodo(inicio: str, fim: str, filtros: dict | None = None,
                   agora_: str | None = None) -> dict:
    """Compromissos do período (já filtrados), indicadores e avisos.

    Indicadores contam o período inteiro, sem os filtros da tela; cancelados
    nunca entram nos números.
    """
    for valor in (inicio, fim):
        date.fromisoformat(valor)  # ValueError em data inválida
    if inicio > fim:
        raise ValueError("Período inválido.")
    filtros = filtros or {}
    agora_ = agora_ or formato.agora()
    with conectar() as conn:
        pedidos = _pedidos_da_agenda(conn, inicio, fim)
        importados = _historicos_da_agenda(conn, inicio, fim)
        todos = [c for p in pedidos for c in _compromissos_do_pedido(p, inicio, fim, agora_)]
        todos += [_compromisso_importado(h) for h in importados]
        _marcar_choques_de_responsavel(todos)
        _marcar_faltas_de_estoque(conn, pedidos, todos)
        sem_data = [dict(r) for r in conn.execute(
            "SELECT p.id, c.nome AS cliente_nome FROM pedidos p"
            " LEFT JOIN clientes c ON c.id = p.cliente_id"
            f" WHERE {_em_operacao('p')} AND p.historico = 0"
            " AND p.data_evento IS NULL AND p.data_retirada IS NULL"
            " AND p.data_devolucao IS NULL ORDER BY p.id").fetchall()]
    for c in todos:
        c["nivel_alerta"] = _nivel_alerta(c)
    todos.sort(key=lambda c: (c["data"], _ordem_no_dia(c)))

    validos = [c for c in todos if not c["cancelado"]]
    kpis = {t: sum(1 for c in validos if c["tipo"] == t)
            for t in ("evento", "retirada", "entrega", "devolucao", "historico")}
    kpis["eventos"] = kpis["evento"] + kpis["historico"]
    kpis["atrasados"] = sum(1 for c in validos if c["atrasado"])
    kpis["pedidos_com_conflito"] = len({c["pedido_id"] for c in validos
                                        if c["nivel_alerta"] == "conflito"})

    lista = [c for c in todos if _passa_filtros_agenda(c, filtros)]
    por_dia: dict = {}
    for c in lista:
        por_dia.setdefault(c["data"], []).append(c)
    return {"inicio": inicio, "fim": fim, "compromissos": lista, "por_dia": por_dia,
            "kpis": kpis, "total_periodo": len(validos), "sem_data": sem_data,
            "filtrado": any(filtros.values()), **_opcoes_de_filtro(pedidos)}
