"""Camada de dados — SQLite."""

import contextvars
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
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
    "forma_pagamento", "condicao_pagamento", "canal", "hora_evento",
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
    backup_antes_sprint51()
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
        _migrar_login(conn)
        _migrar_catalogo(conn)
        _migrar_sprint51(conn)
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
    # Identidade visual (cores centrais: login e, no futuro, o sistema)
    "cor_primaria": "#6F1C85", "cor_secundaria": "#FF8C00",
    # Página de login (Personalização do Login). Botão vazio = cor primária.
    "login_layout": "dividido", "login_modelo": "padrao", "login_imagem": "",
    "login_cor_botao": "", "login_cor_fundo": "#FFFFFF",
    "login_cor_texto": "#1E1C22", "login_cor_texto_sec": "#6B6472",
    "login_titulo": "Bem-vindo(a)",
    "login_subtitulo": "Acesse sua conta e continue gerenciando as festas com praticidade.",
    "login_slogan": "SEU MOMENTO, SUA FESTA",
    "login_mensagem": "Organize pedidos, agenda e operação das suas festas em um só lugar.",
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


# Validade do cache: com mais de um processo no servidor, a mudança feita em um
# chega aos outros em até este tempo (no mesmo processo, na hora).
CONFIG_CACHE_SEGUNDOS = 60


def config(chave: str, padrao: str | None = None) -> str:
    """Configuração da empresa atual (ou o padrão do sistema)."""
    tid = tenant_atual()
    guardado = _config_cache.get((CAMINHO_BD, tid))
    if guardado is None or time.monotonic() - guardado[0] > CONFIG_CACHE_SEGUNDOS:
        with conectar() as conn:
            cache = {r["chave"]: r["valor"] for r in conn.execute(
                "SELECT chave, valor FROM configuracoes WHERE tenant_id = ?", (tid,))}
        _config_cache[(CAMINHO_BD, tid)] = (time.monotonic(), cache)
    else:
        cache = guardado[1]
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


# Erro de validação ligado a um campo do formulário (definido junto das regras
# de itens para que regras e dados levantem a mesma exceção).
from sistema import regras_itens as regras  # noqa: E402
ErroDeCampo = regras.ErroDeCampo


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
            "INSERT INTO categorias (tenant_id, nome, pai_id, criado_em, slug)"
            " VALUES (?, ?, ?, ?, ?)", (tenant_atual(), nome, pai_id, formato.agora(),
                                        _slug_livre(conn, tenant_atual(), nome, "categorias")))
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
        if conn.execute("SELECT COUNT(*) FROM kits WHERE categoria_id = ?", (id_,)).fetchone()[0]:
            raise ErroDeCampo("nome", "Categoria em uso por kits.")
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
        p["em_kits"] = conn.execute("SELECT COUNT(DISTINCT kit_id) FROM itens_kit WHERE produto_id = ?",
                                    (p["id"],)).fetchone()[0]
        p["em_pedidos"] = conn.execute(
            "SELECT COUNT(DISTINCT pedido_id) FROM itens_pedido WHERE tipo = 'produto' AND item_id = ?",
            (p["id"],)).fetchone()[0]
        return p


def _tags_do_produto(conn, produto_id: int) -> list:
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags_produto WHERE produto_id = ? ORDER BY tag",
        (produto_id,)).fetchall()]


def _fotos_do_produto(conn, produto_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM fotos_produto WHERE produto_id = ? ORDER BY principal DESC, ordem, id",
        (produto_id,)).fetchall()]


def _foto_capa(conn, produto_id: int) -> dict | None:
    r = conn.execute(
        "SELECT * FROM fotos_produto WHERE produto_id = ? ORDER BY principal DESC, ordem, id LIMIT 1",
        (produto_id,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Fotos de produto
# ---------------------------------------------------------------------------

def salvar_foto_produto(produto_id: int, arquivo: str, principal: bool = False,
                        miniatura: str = "") -> int:
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
        ordem = conn.execute("SELECT COALESCE(MAX(ordem), 0) + 1 FROM fotos_produto"
                             " WHERE produto_id = ?", (produto_id,)).fetchone()[0]
        r = conn.execute(
            "INSERT INTO fotos_produto (produto_id, arquivo, principal, criado_em,"
            " miniatura, ordem) VALUES (?, ?, ?, ?, ?, ?)",
            (produto_id, arquivo, is_principal, formato.agora(), miniatura, ordem))
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
    sql = ("SELECT k.*, c.nome AS categoria_nome FROM kits k"
           " LEFT JOIN categorias c ON c.id = k.categoria_id"
           f" WHERE {_t('k')}")
    params = []
    if status:
        sql += " AND k.status = ?"
        params.append(status)
    sql += " ORDER BY k.nome"
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
        r = conn.execute("SELECT k.*, c.nome AS categoria_nome FROM kits k"
                         " LEFT JOIN categorias c ON c.id = k.categoria_id"
                         f" WHERE k.id = ? AND {_t('k')}", (id_,)).fetchone()
        if not r:
            return None
        k = dict(r)
        k["tags"] = [t["tag"] for t in conn.execute(
            "SELECT tag FROM tags_kit WHERE kit_id = ? ORDER BY tag", (id_,))]
        k["itens"] = _itens_do_kit(conn, k["id"])
        k["fotos"] = _fotos_do_kit(conn, k["id"])
        k["foto_capa"] = _foto_capa_kit(conn, k["id"])
        k["soma_produtos"] = sum(
            (i.get("preco_locacao") or 0) * i["quantidade"]
            for i in k["itens"] if i.get("obrigatorio", 1))
        k["em_pedidos"] = conn.execute(
            "SELECT COUNT(DISTINCT pedido_id) FROM itens_pedido WHERE tipo = 'kit' AND item_id = ?",
            (k["id"],)).fetchone()[0]
        return k


def _itens_do_kit(conn, kit_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT ik.*, p.nome AS produto_nome, p.codigo_sku, p.tipo AS produto_tipo, p.unidade,"
        " p.preco_locacao, p.quantidade_total, p.status AS produto_status"
        " FROM itens_kit ik"
        " JOIN produtos p ON p.id = ik.produto_id"
        " WHERE ik.kit_id = ? ORDER BY ik.obrigatorio DESC, p.nome",
        (kit_id,)).fetchall()]


def _fotos_do_kit(conn, kit_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM fotos_kit WHERE kit_id = ? ORDER BY principal DESC, ordem, id",
        (kit_id,)).fetchall()]


def _foto_capa_kit(conn, kit_id: int) -> dict | None:
    r = conn.execute(
        "SELECT * FROM fotos_kit WHERE kit_id = ? ORDER BY principal DESC, ordem, id LIMIT 1",
        (kit_id,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Fotos de kit
# ---------------------------------------------------------------------------

def salvar_foto_kit(kit_id: int, arquivo: str, principal: bool = False,
                    miniatura: str = "") -> int:
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
        ordem = conn.execute("SELECT COALESCE(MAX(ordem), 0) + 1 FROM fotos_kit"
                             " WHERE kit_id = ?", (kit_id,)).fetchone()[0]
        r = conn.execute(
            "INSERT INTO fotos_kit (kit_id, arquivo, principal, criado_em, miniatura, ordem)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kit_id, arquivo, is_principal, formato.agora(), miniatura, ordem))
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
    sql = f"SELECT k.* FROM kits k WHERE {_KIT_PUBLICO} AND {_t('k')} ORDER BY k.nome"
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
            "SELECT k.*, c.nome AS categoria_nome FROM kits k"
            " LEFT JOIN categorias c ON c.id = k.categoria_id"
            f" WHERE k.id = ? AND {_KIT_PUBLICO} AND {_t('k')}",
            (id_,)).fetchone()
        if not r:
            return None
        k = dict(r)
        k["tags"] = [t["tag"] for t in conn.execute(
            "SELECT tag FROM tags_kit WHERE kit_id = ? ORDER BY tag", (id_,))]
        k["itens"] = _itens_do_kit(conn, k["id"])
        k["fotos"] = _fotos_do_kit(conn, k["id"])
        k["foto_capa"] = _foto_capa_kit(conn, k["id"])
        k["soma_produtos"] = sum(
            (i.get("preco_locacao") or 0) * i["quantidade"]
            for i in k["itens"] if i.get("obrigatorio", 1))
        return k


# ---------------------------------------------------------------------------
# Vitrine pública (Sprint 7): consome o mesmo cadastro, estoque e regras.
# Nada aqui reserva estoque; a reserva só acontece quando o pedido é salvo.
# ---------------------------------------------------------------------------

VITRINE_POR_PAGINA = 24
ORCAMENTO_MAX_ITENS = 30
ORCAMENTO_MAX_QTD = 99

# Kit só é público com pelo menos um componente (sem peças não há o que alugar)
_KIT_PUBLICO = ("k.status = 'ativo' AND k.publicado = 1"
                " AND EXISTS (SELECT 1 FROM itens_kit ik WHERE ik.kit_id = k.id)")
_PRODUTO_PUBLICO = "p.status = 'disponivel' AND p.publicado = 1"


def _ids_da_categoria(conn, categoria_id) -> list:
    """A categoria e as filhas diretas (a vitrine mostra o tema inteiro)."""
    if not categoria_id:
        return []
    return [categoria_id] + [r["id"] for r in conn.execute(
        f"SELECT id FROM categorias WHERE pai_id = ? AND {_t()}", (categoria_id,))]


def vitrine_itens(tipo: str = "", categoria_id=None, busca: str = "",
                  somente_destaques: bool = False) -> list:
    """Produtos e kits visíveis ao público, numa lista só (2 consultas)."""
    with conectar() as conn:
        cats = _ids_da_categoria(conn, categoria_id)
        filtro_cat = (f" AND {{a}}.categoria_id IN ({','.join('?' * len(cats))})"
                      if cats else "")
        itens = []
        if tipo in ("", "produto"):
            itens += [dict(r, tipo="produto") for r in conn.execute(
                "SELECT p.id, p.nome, p.slug, p.descricao, p.preco_locacao AS preco, p.selo,"
                " p.destaque, p.categoria_id, c.nome AS categoria_nome, 0 AS economia,"
                f" {_capa_sql('fotos_produto', 'produto_id', 'p')} AS capa,"
                " (SELECT GROUP_CONCAT(tag, ' ') FROM tags_produto t WHERE t.produto_id = p.id) AS tags"
                " FROM produtos p LEFT JOIN categorias c ON c.id = p.categoria_id"
                f" WHERE {_PRODUTO_PUBLICO} AND {_t('p')}" + filtro_cat.format(a="p"), cats)]
        if tipo in ("", "kit"):
            for r in conn.execute(
                    "SELECT k.id, k.nome, k.slug, k.descricao, k.preco, k.selo, k.destaque,"
                    " k.categoria_id, c.nome AS categoria_nome,"
                    f" {_capa_sql('fotos_kit', 'kit_id', 'k')} AS capa,"
                    " (SELECT GROUP_CONCAT(tag, ' ') FROM tags_kit t WHERE t.kit_id = k.id) AS tags,"
                    " (SELECT SUM(ik.quantidade * p.preco_locacao) FROM itens_kit ik"
                    "   JOIN produtos p ON p.id = ik.produto_id WHERE ik.kit_id = k.id AND ik.obrigatorio = 1) AS soma"
                    " FROM kits k LEFT JOIN categorias c ON c.id = k.categoria_id"
                    f" WHERE {_KIT_PUBLICO} AND {_t('k')}" + filtro_cat.format(a="k"), cats):
                k = dict(r, tipo="kit")
                soma = k.pop("soma") or 0
                k["economia"] = round((1 - k["preco"] / soma) * 100) if soma and 0 < k["preco"] < soma else 0
                itens.append(k)
    termo = normalizar_texto(busca).strip()
    if termo:
        palavras = termo.split()
        itens = [i for i in itens if all(
            w in normalizar_texto(" ".join(str(i.get(c) or "") for c in (
                "nome", "descricao", "tags", "categoria_nome"))) for w in palavras)]
    if somente_destaques:
        itens = [i for i in itens if i["destaque"] or i["selo"]]
    return sorted(itens, key=lambda i: (not i["destaque"], i["tipo"] != "kit",
                                        normalizar_texto(i["nome"])))


def paginar(itens: list, pagina: int, por_pagina: int = VITRINE_POR_PAGINA) -> dict:
    total = len(itens)
    paginas = max(1, -(-total // por_pagina))
    pagina = min(max(1, int(pagina or 1)), paginas)
    ini = (pagina - 1) * por_pagina
    return {"itens": itens[ini:ini + por_pagina], "pagina": pagina, "paginas": paginas,
            "total": total}


def vitrine_categorias() -> list:
    """Categorias principais visíveis que têm algo publicado (com a contagem)."""
    with conectar() as conn:
        cats = [dict(r) for r in conn.execute(
            "SELECT id, nome, slug, imagem, descricao, pai_id FROM categorias"
            f" WHERE COALESCE(visivel, 1) = 1 AND {_t()} ORDER BY ordem, nome")]
        contagem: dict = {}
        for sql in ("SELECT p.categoria_id AS c, COUNT(*) AS n FROM produtos p"
                    f" WHERE {_PRODUTO_PUBLICO} AND {_t('p')} GROUP BY p.categoria_id",
                    "SELECT k.categoria_id AS c, COUNT(*) AS n FROM kits k"
                    f" WHERE {_KIT_PUBLICO} AND {_t('k')} GROUP BY k.categoria_id"):
            for r in conn.execute(sql):
                contagem[r["c"]] = contagem.get(r["c"], 0) + r["n"]
    visiveis = {c["id"] for c in cats}
    for c in cats:
        c["quantidade"] = contagem.get(c["id"], 0) + sum(
            contagem.get(f["id"], 0) for f in cats if f["pai_id"] == c["id"])
    return [c for c in cats if c["quantidade"] and (not c["pai_id"] or c["pai_id"] not in visiveis)]


def _item_publico(conn, tipo: str, id_) -> dict | None:
    """Registro público mínimo (nome, preço atual, slug, capa) ou None."""
    try:
        id_ = int(id_)
    except (TypeError, ValueError):
        return None
    if tipo == "produto":
        r = conn.execute(
            "SELECT p.id, p.nome, p.slug, p.preco_locacao AS preco, p.unidade, p.qtd_minima,"
            f" {_capa_sql('fotos_produto', 'produto_id', 'p')} AS capa"
            f" FROM produtos p WHERE p.id = ? AND {_PRODUTO_PUBLICO} AND {_t('p')}", (id_,)).fetchone()
    elif tipo == "kit":
        r = conn.execute(
            "SELECT k.id, k.nome, k.slug, k.preco, 'pacote' AS unidade, NULL AS qtd_minima,"
            f" {_capa_sql('fotos_kit', 'kit_id', 'k')} AS capa"
            f" FROM kits k WHERE k.id = ? AND {_KIT_PUBLICO} AND {_t('k')}", (id_,)).fetchone()
    else:
        return None
    return dict(r, tipo=tipo) if r else None


def _itens_do_carrinho(itens) -> list:
    """Normaliza [{tipo, id, quantidade}] vindo do navegador (nunca confia no preço)."""
    if not isinstance(itens, list):
        raise ValueError("Lista de itens inválida.")
    juntos: dict = {}
    for i in itens[:ORCAMENTO_MAX_ITENS * 2]:
        if not isinstance(i, dict) or i.get("tipo") not in TIPOS_CATALOGO:
            continue
        try:
            id_, qtd = int(i.get("id")), round(regras.numero(i.get("quantidade") or 1), regras.CASAS)
        except (TypeError, ValueError):
            continue
        if id_ <= 0 or qtd <= 0:
            continue
        chave = (i["tipo"], id_)
        juntos[chave] = min(juntos.get(chave, 0) + qtd, ORCAMENTO_MAX_QTD)
    return [{"tipo": t, "id": id_, "quantidade": q} for (t, id_), q in juntos.items()][:ORCAMENTO_MAX_ITENS]


def resumo_orcamento_publico(itens, data_festa: str = "", hoje: str | None = None) -> dict:
    """Confere a lista do visitante: preço atual, situação na data e o conjunto.

    Só consulta. Nada é reservado: a reserva acontece quando o pedido é salvo.
    """
    pedidos = _itens_do_carrinho(itens)
    if data_festa:
        date.fromisoformat(data_festa)  # ValueError em data inválida
    linhas, faltas = [], []
    with conectar() as conn:
        for p in pedidos:
            reg = _item_publico(conn, p["tipo"], p["id"])
            if not reg:
                continue  # saiu da vitrine: some da lista, sem erro
            qtd = p["quantidade"]
            if not regras.aceita_fracao(reg["unidade"]):
                qtd = max(1, math.ceil(qtd - 1e-9))  # unidade inteira: arredonda para cima
            qtd = max(qtd, reg["qtd_minima"] or 0)
            qtd = int(qtd) if qtd == int(qtd) else round(qtd, regras.CASAS)
            linhas.append(dict(reg, quantidade=qtd, sigla=regras.sigla(reg["unidade"]),
                               subtotal=round(reg["preco"] * qtd, 2)))
        if data_festa and linhas:
            janelas = []
            for ln in linhas:
                sit = situacao_na_data(ln["tipo"], ln["id"], data_festa, ln["quantidade"], hoje)
                ln["situacao"] = sit["situacao"] if sit else "indisponivel"
                ln["rotulo"] = ROTULO_SITUACAO[ln["situacao"]]
                ln["motivo"] = (sit or {}).get("motivo", "")
                ln["livres"] = (sit or {}).get("livres", 0)
                janelas.append((sit or {}).get("retirada", data_festa))
                janelas.append((sit or {}).get("devolucao", data_festa))
            # o conjunto: peças repetidas entre kits e avulsos disputam o mesmo estoque
            faltas = _faltas_de_estoque(
                conn, [{"tipo": ln["tipo"], "item_id": ln["id"], "quantidade": ln["quantidade"]}
                       for ln in linhas], min(janelas), max(janelas))
    return {"itens": linhas, "total": round(sum(ln["subtotal"] for ln in linhas), 2),
            "data_evento": data_festa, "faltas": faltas,
            "disponivel": bool(linhas) and not faltas and all(
                ln.get("situacao", "disponivel") != "indisponivel" for ln in linhas)}


def _cliente_por_whatsapp(conn, whatsapp: str):
    """Mesmo cliente se o WhatsApp bater (com ou sem 55 e sem símbolos)."""
    digitos = somente_digitos(whatsapp)
    variantes = {digitos, digitos[2:] if digitos.startswith("55") else "55" + digitos}
    for r in conn.execute(f"SELECT id, whatsapp FROM clientes WHERE whatsapp != '' AND {_t()}"
                          " ORDER BY id"):
        if somente_digitos(r["whatsapp"]) in variantes:
            return r["id"]
    return None


def solicitar_orcamento_catalogo(contato: dict, itens, data_festa: str,
                                 hoje: str | None = None) -> dict:
    """Pedido de orçamento vindo da vitrine: cliente (sem duplicar), lead e
    orçamento em rascunho no fluxo comercial. Não reserva estoque."""
    nome = " ".join(str(contato.get("nome") or "").split())[:120]
    whatsapp = _limpar_fone(str(contato.get("whatsapp") or ""))[:20]
    email = str(contato.get("email") or "").strip().lower()[:160]
    obs = str(contato.get("observacoes") or "").strip()[:1000]
    if not nome:
        raise ErroDeCampo("nome", "Informe seu nome.")
    if len(somente_digitos(whatsapp)) < 10:
        raise ErroDeCampo("whatsapp", "Informe um WhatsApp com DDD.")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ErroDeCampo("email", "E-mail inválido.")
    hoje = hoje or _hoje_iso()
    try:
        date.fromisoformat(data_festa or "")
    except ValueError:
        raise ErroDeCampo("data_evento", "Escolha a data da festa.")
    if data_festa < hoje:
        raise ErroDeCampo("data_evento", "A data da festa já passou.")
    resumo = resumo_orcamento_publico(itens, data_festa, hoje)
    if not resumo["itens"]:
        raise ErroDeCampo("itens", "Sua lista está vazia.")
    indisponiveis = [ln["nome"] for ln in resumo["itens"] if ln["situacao"] == "indisponivel"]
    if indisponiveis:
        raise ErroDeCampo("itens", "Indisponível para esta data: " + ", ".join(indisponiveis)
                          + ". Remova da lista ou escolha outra data.")
    if resumo["faltas"]:
        raise ErroDeCampo("itens", "Não há peças suficientes para tudo junto nesta data. "
                          + " ".join(resumo["faltas"]))
    agora_ = formato.agora()
    descricao = "; ".join(f"{ln['quantidade']}x {ln['nome']}" for ln in resumo["itens"])
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")  # dois envios iguais não criam dois clientes
        cliente_id = _cliente_por_whatsapp(conn, whatsapp)
        novo_cliente = cliente_id is None
        if novo_cliente:
            if email and conn.execute(f"SELECT 1 FROM clientes WHERE email = ? AND {_t()}",
                                      (email,)).fetchone():
                email_cli = ""  # e-mail já é de outro cadastro: não duplica nem sobrescreve
            else:
                email_cli = email
            cliente_id = conn.execute(
                "INSERT INTO clientes (tenant_id, nome, whatsapp, email, cidade, origem, status,"
                " criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_atual(), nome, whatsapp, email_cli, "Campo Grande", "Catálogo", "ativo",
                 agora_, agora_)).lastrowid
        origem = conn.execute(f"SELECT id FROM origens_lead WHERE nome = 'Catálogo' AND {_t()}"
                              ).fetchone()
        origem_id = origem["id"] if origem else conn.execute(
            "INSERT INTO origens_lead (nome, tenant_id) VALUES ('Catálogo', ?)",
            (tenant_atual(),)).lastrowid
        texto_obs = "Pedido pela vitrine." + (f" Cliente escreveu: {obs}" if obs else "")
        if email and not novo_cliente:
            texto_obs += f" E-mail informado: {email}"
        lead_id = conn.execute(
            "INSERT INTO leads (tenant_id, cliente_id, origem_id, interesse, valor_estimado,"
            " status, data_evento, observacoes, criado_em, atualizado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tenant_atual(), cliente_id, origem_id, descricao[:500], resumo["total"], "novo",
             data_festa, texto_obs, agora_, agora_)).lastrowid
        orc_id = conn.execute(
            "INSERT INTO orcamentos (tenant_id, lead_id, cliente_id, desconto, observacoes,"
            " status, data_evento, origem, criado_em, atualizado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tenant_atual(), lead_id, cliente_id, 0, texto_obs, "rascunho", data_festa,
             "catalogo", agora_, agora_)).lastrowid
        _inserir_linhas(conn, "itens_orcamento", "orcamento_id", orc_id, _preparar_linhas(
            conn, [{"tipo": ln["tipo"], "item_id": ln["id"], "descricao": ln["nome"],
                    "quantidade": ln["quantidade"], "preco_unitario": ln["preco"]}
                   for ln in resumo["itens"]]))
        auditar(conn, "orcamento", orc_id, "criar",
                f"Orçamento #{orc_id} pedido pela vitrine ({len(resumo['itens'])} itens)", None,
                tipo="orcamento_catalogo",
                dados={"cliente_id": cliente_id, "cliente_novo": novo_cliente,
                       "lead_id": lead_id, "total": resumo["total"], "data_evento": data_festa})
    return {"orcamento_id": orc_id, "lead_id": lead_id, "cliente_id": cliente_id,
            "cliente_novo": novo_cliente, "total": resumo["total"], "itens": resumo["itens"]}


def publico_por_slug(slug: str):
    """Resolve /catalogo/<slug>: ("categoria"|"produto"|"kit", registro) ou None.

    Só devolve o que o público pode ver (publicado, ativo; categoria visível).
    """
    slug = (slug or "").strip().lower()
    if not slug or len(slug) > 80:
        return None
    with conectar() as conn:
        r = conn.execute(f"SELECT id FROM categorias WHERE slug = ? AND COALESCE(visivel, 1) = 1"
                         f" AND {_t()}", (slug,)).fetchone()
        if r:
            return "categoria", dict(conn.execute(
                "SELECT * FROM categorias WHERE id = ?", (r["id"],)).fetchone())
        r = conn.execute(f"SELECT id FROM produtos WHERE slug = ? AND {_t()}", (slug,)).fetchone()
        if r:
            p = produto_publico(r["id"])
            return ("produto", p) if p else None
        r = conn.execute(f"SELECT id FROM kits WHERE slug = ? AND {_t()}", (slug,)).fetchone()
        if r:
            k = kit_publico(r["id"])
            return ("kit", k) if k else None
    return None


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
    data_evento = dados_.get("data_evento") or None
    if data_evento:
        try:
            date.fromisoformat(data_evento)
        except ValueError:
            raise ErroDeCampo("data_evento", "Data da festa inválida.")

    agora_ = formato.agora()
    itens = _validar_itens(itens)
    with conectar() as conn:
        _validar_referencias(conn, dados_)
        _validar_itens_da_empresa(conn, itens)
        anterior = None
        if id_:
            anterior = conn.execute(f"SELECT * FROM orcamentos WHERE id = ? AND {_t()}",
                                    (id_,)).fetchone()
            if not anterior:
                raise ValueError("Orçamento não encontrado.")
        linhas_antigas = [dict(r) for r in conn.execute(
            "SELECT * FROM itens_orcamento WHERE orcamento_id = ?", (id_,))] if id_ else []
        itens = _preparar_linhas(conn, itens, linhas_antigas)
        modalidade = dados_.get("modalidade")
        if modalidade is None:
            modalidade = (anterior["modalidade"] if anterior else "") or ""
        _validar_modalidade_venda(conn, modalidade, itens)
        if id_:
            conn.execute(
                "UPDATE orcamentos SET lead_id=?, cliente_id=?, desconto=?,"
                " observacoes=?, status=?, data_evento=?, modalidade=?, atualizado_em=?"
                f" WHERE id=? AND {_t()}",
                (dados_.get("lead_id"), dados_["cliente_id"], desconto,
                 dados_.get("observacoes", ""), status, data_evento, modalidade, agora_, id_))
            conn.execute("DELETE FROM itens_orcamento WHERE orcamento_id=?",
                         (id_,))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO orcamentos (tenant_id, lead_id, cliente_id, desconto,"
                " observacoes, status, data_evento, origem, modalidade, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_atual(), dados_.get("lead_id"), dados_["cliente_id"], desconto,
                 dados_.get("observacoes", ""), status, data_evento,
                 dados_.get("origem", ""), modalidade, agora_, agora_))
            novo_id = r.lastrowid
        _inserir_linhas(conn, "itens_orcamento", "orcamento_id", novo_id, itens)
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
    data_evento = orc.get("data_evento") or None
    with conectar() as conn:
        # a conversão já ocupa estoque na data da festa: confere antes (com trava)
        conn.execute("BEGIN IMMEDIATE")
        if data_evento:
            faltas = _faltas_de_estoque(conn, orc["itens"], data_evento, data_evento)
            if faltas:
                raise ValueError(faltas[0])
        r = conn.execute(
            "INSERT INTO pedidos (tenant_id, orcamento_id, cliente_id, data_evento,"
            " observacoes, modalidade, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?)",
            (tenant_atual(), orcamento_id, orc["cliente_id"], data_evento,
             orc.get("observacoes", ""), orc.get("modalidade") or "", agora_, agora_))
        pedido_id = r.lastrowid
        # mesmos preços, unidades e composição do orçamento aceito
        _inserir_linhas(conn, "itens_pedido", "pedido_id", pedido_id,
                        _preparar_linhas(conn, orc["itens"]))
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
    d["modalidade"] = (form.get("modalidade") or "").strip() if "modalidade" in form else None
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
            qtd = round(regras.numero(item.get("quantidade") or 1), regras.CASAS)
            preco = float(str(item.get("preco_unitario") or 0).replace(",", "."))
        except ValueError:
            raise ErroDeCampo(campo, f"Quantidade ou valor inválido em '{descricao}'.")
        if qtd <= 0:
            raise ErroDeCampo(campo, f"Quantidade de '{descricao}' deve ser maior que zero.")
        qtd = int(qtd) if qtd == int(qtd) else qtd
        if preco < 0:
            raise ErroDeCampo(campo, f"Valor de '{descricao}' não pode ser negativo.")
        validos.append({"tipo": tipo, "item_id": item.get("item_id"),
                        "descricao": descricao, "quantidade": qtd,
                        "preco_unitario": round(preco, 2),
                        "composicao": item.get("composicao") or ""})
    return validos


# --- Linhas de venda (orçamento e pedido) — Sprint 5.1 -------------------------

def _composicao_venda(conn, kit_id) -> list:
    """Retrato da composição do kit no momento da venda (gravado na linha)."""
    return [{"produto_id": c["produto_id"], "nome": c["nome"], "quantidade": c["quantidade"],
             "unidade": c["unidade"], "tipo": c["tipo"], "obrigatorio": c["obrigatorio"],
             "preco": c["preco"], "estoque": 1 if regras.consome_estoque(c["tipo"]) else 0}
            for c in _componentes(conn, kit_id)]


def _ler_composicao(valor) -> list:
    if isinstance(valor, list):
        return valor
    try:
        dado = json.loads(valor) if valor else []
    except (TypeError, ValueError):
        return []
    return dado if isinstance(dado, list) else []


def _preparar_linhas(conn, itens: list, anteriores: list | None = None) -> list:
    """Confere a quantidade pela unidade do item e grava unidade e composição.

    Ao editar, uma linha de kit que já existia mantém a composição da venda
    original: mudar o kit depois não altera pedidos e orçamentos registrados.
    """
    antigas = {}
    for a in anteriores or []:
        if a.get("tipo") == "kit" and a.get("item_id") and a.get("composicao"):
            antigas.setdefault(int(a["item_id"]), a["composicao"])
    saida = []
    for n, item in enumerate(itens):
        linha = dict(item)
        unidade, campo = "", f"item_descricao_{n}"
        if item.get("tipo") == "produto" and item.get("item_id"):
            r = conn.execute(f"SELECT nome, unidade, qtd_minima FROM produtos WHERE id = ? AND {_t()}",
                             (item["item_id"],)).fetchone()
            if r:
                unidade = r["unidade"] or "unidade"
                try:
                    linha["quantidade"] = regras.quantidade(item["quantidade"], unidade, campo)
                except ErroDeCampo as e:
                    raise ErroDeCampo(campo, f"{item.get('descricao') or r['nome']}: {e}")
        elif item.get("tipo") == "kit" and item.get("item_id"):
            unidade = "pacote"
            linha["quantidade"] = regras.quantidade(item["quantidade"], "pacote", campo)
            comp = item.get("composicao") or antigas.get(int(item["item_id"]))
            if not comp:
                comp = json.dumps(_composicao_venda(conn, item["item_id"]), ensure_ascii=False)
            elif isinstance(comp, list):
                comp = json.dumps(comp, ensure_ascii=False)
            linha["composicao"] = comp
        linha["unidade"] = unidade or item.get("unidade") or ""
        linha.setdefault("composicao", "")
        if item.get("tipo") != "kit":
            linha["composicao"] = ""
        saida.append(linha)
    return saida


def _inserir_linhas(conn, tabela: str, coluna: str, dono_id: int, linhas: list):
    for ln in linhas:
        conn.execute(
            f"INSERT INTO {tabela} ({coluna}, tipo, item_id, descricao, quantidade,"
            " preco_unitario, unidade, composicao) VALUES (?,?,?,?,?,?,?,?)",
            (dono_id, ln.get("tipo", "produto"), ln.get("item_id"), ln["descricao"].strip(),
             ln["quantidade"], float(ln.get("preco_unitario") or 0), ln.get("unidade") or "",
             ln.get("composicao") or ""))


def modalidades_dos_itens(conn, itens: list) -> tuple:
    """(modalidades aceitas por todos os itens, [(nome, restrição)])."""
    possiveis, restricoes = set(regras.MODALIDADES), []
    for item in itens:
        if not item.get("item_id"):
            continue
        if item.get("tipo") == "produto":
            r = conn.execute(f"SELECT * FROM produtos WHERE id = ? AND {_t()}",
                             (item["item_id"],)).fetchone()
            if r:
                aceitas = regras.modalidades_do_produto(dict(r))
                for m in sorted(possiveis - aceitas):
                    restricoes.append(f"'{r['nome']}' não permite {regras.MODALIDADES[m].lower()}.")
                possiveis &= aceitas
        elif item.get("tipo") == "kit":
            k = conn.execute(f"SELECT * FROM kits WHERE id = ? AND {_t()}",
                             (item["item_id"],)).fetchone()
            if k:
                comps = _ler_composicao(item.get("composicao")) or _componentes(conn, k["id"])
                for c in comps:  # o retrato não guarda as modalidades: usa o cadastro
                    p = conn.execute("SELECT * FROM produtos WHERE id = ?", (c["produto_id"],)).fetchone()
                    if p:
                        c.update({x: p[x] for x in ("permite_retirada", "permite_montagem",
                                                    "local_execucao")}, tipo=p["tipo"])
                aceitas, rest = regras.modalidades_do_kit(dict(k), comps)
                restricoes += [f"{k['nome']}: {x}" for x in rest]
                if not k["permite_retirada"] and "retirada" in possiveis:
                    restricoes.append(f"'{k['nome']}' não permite retirada pelo cliente.")
                if not k["permite_montagem"] and "montagem" in possiveis:
                    restricoes.append(f"'{k['nome']}' não permite montagem no local.")
                possiveis &= aceitas
    return possiveis, restricoes


def _validar_modalidade_venda(conn, modalidade, itens: list):
    if not modalidade:
        return
    if modalidade not in regras.MODALIDADES:
        raise ErroDeCampo("modalidade", "Modalidade de atendimento inválida.")
    possiveis, restricoes = modalidades_dos_itens(conn, itens)
    if modalidade not in possiveis:
        rotulo = regras.MODALIDADES[modalidade].lower()
        motivo = " ".join(x for x in restricoes if rotulo in x) or " ".join(restricoes)
        raise ErroDeCampo("modalidade", f"Não é possível atender com {rotulo}. {motivo}".strip())


def _validar_dados_pedido(d: dict):
    for campo in ("data_evento", "data_retirada", "data_devolucao"):
        if d.get(campo):
            try:
                date.fromisoformat(d[campo])
            except ValueError:
                raise ErroDeCampo(campo, "Data inválida.")
    for campo in ("hora_retirada", "hora_devolucao", "hora_evento"):
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
    for campo, hora, depois, mensagem in (
            ("data_retirada", "hora_retirada", False,
             "No dia da festa, a saída precisa ser antes do horário do evento."),
            ("data_devolucao", "hora_devolucao", True,
             "No dia da festa, a devolução precisa ser depois do horário do evento.")):
        if (ev and d.get(campo) == ev and d.get(hora) and d.get("hora_evento")
                and (d[hora] < d["hora_evento"] if depois else d[hora] > d["hora_evento"])):
            raise ErroDeCampo(hora, mensagem)


def _validar_itens_da_empresa(conn, itens: list):
    """Produto ou kit citado num item precisa ser da empresa atual."""
    for item in itens:
        tabela = {"produto": "produtos", "kit": "kits"}.get(item.get("tipo"))
        if tabela and item.get("item_id") and not _do_tenant(
                conn, tabela, item["item_id"]):
            raise ErroDeCampo("item_descricao_0",
                              f"Item não encontrado: {item.get('descricao', '')}.")


def _assinatura_itens(itens: list) -> list:
    return sorted(f"{i['tipo']}:{i['descricao']} x{regras.formatar_qtd(i['quantidade'])}"
                  f" @ {float(i['preco_unitario']):.2f}" for i in itens)



def _verificar_disponibilidade_itens(conn, itens: list, data_retirada: str,
                                     data_devolucao: str,
                                     pedido_id: int | None = None):
    faltas = _faltas_de_estoque(conn, itens, data_retirada, data_devolucao, pedido_id)
    if faltas:
        raise ErroDeCampo("item_descricao_0", faltas[0])


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

    data_ret = dados_.get("data_retirada") or None
    data_dev = dados_.get("data_devolucao") or None
    agora_ = formato.agora()
    extras = [dados_.get(c, "") for c in CAMPOS_TEXTO_PEDIDO]
    # Histórico é pedido encerrado: se o administrador reabre o status,
    # a marca sai junto (Sprint 2.1).
    historico = int(bool(atual and atual["historico"] and sc in FORA_DA_OPERACAO))

    with conectar() as conn:
        # Trava de escrita antes de conferir o estoque: duas gravações ao mesmo
        # tempo esperam uma pela outra e a segunda já vê a primeira reserva.
        conn.execute("BEGIN IMMEDIATE")
        ret_estoque = data_ret or dados_.get("data_evento")
        dev_estoque = data_dev or dados_.get("data_evento") or data_ret
        itens = _preparar_linhas(conn, itens, (atual or {}).get("itens"))
        modalidade = dados_.get("modalidade")
        if modalidade is None:  # formulário sem o campo: mantém
            modalidade = (atual or {}).get("modalidade") or ""
        if modalidade != (atual or {}).get("modalidade", ""):
            _validar_modalidade_venda(conn, modalidade, itens)
        if ret_estoque and dev_estoque and sc not in FORA_DA_OPERACAO and not historico:
            _verificar_disponibilidade_itens(conn, itens, ret_estoque, dev_estoque, id_)

        if atual:
            conn.execute(
                "UPDATE pedidos SET cliente_id=?, data_evento=?,"
                " data_retirada=?, data_devolucao=?,"
                " status_comercial=?, status_operacional=?, observacoes=?,"
                + "".join(f" {c}=?," for c in CAMPOS_TEXTO_PEDIDO) +
                f" responsavel_id=?, historico=?, modalidade=?, atualizado_em=? WHERE id=? AND {_t()}",
                (dados_["cliente_id"], dados_.get("data_evento"), data_ret,
                 data_dev, sc, so, dados_.get("observacoes", ""),
                 *extras, dados_.get("responsavel_id"), historico, modalidade, agora_, id_))
            conn.execute("DELETE FROM itens_pedido WHERE pedido_id=?", (id_,))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO pedidos (tenant_id, cliente_id, data_evento, data_retirada,"
                " data_devolucao, status_comercial, status_operacional,"
                " observacoes, "
                + "".join(f"{c}, " for c in CAMPOS_TEXTO_PEDIDO) +
                "responsavel_id, modalidade, criado_em, atualizado_em) VALUES ("
                + ",".join("?" * (12 + len(CAMPOS_TEXTO_PEDIDO))) + ")",
                (tenant_atual(), dados_["cliente_id"], dados_.get("data_evento"), data_ret,
                 data_dev, sc, so, dados_.get("observacoes", ""),
                 *extras, dados_.get("responsavel_id"), modalidade, agora_, agora_))
            novo_id = r.lastrowid

        _inserir_linhas(conn, "itens_pedido", "pedido_id", novo_id, itens)

        if atual:
            novos = dict(dados_, status_comercial=sc, status_operacional=so,
                         data_retirada=data_ret, data_devolucao=data_dev,
                         historico=historico, modalidade=modalidade)
            mudancas = {}
            for campo in ("cliente_id", "data_evento", "data_retirada",
                          "data_devolucao", "status_comercial",
                          "status_operacional", "observacoes", "historico",
                          "responsavel_id", "modalidade", *CAMPOS_TEXTO_PEDIDO):
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
    "hora_evento": "horário do evento", "modalidade": "modalidade de atendimento",
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
                                ("data_evento", "hora_evento", "Festa"),
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
        " p.hora_retirada, p.hora_evento, p.status_comercial, p.status_operacional,"
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
    festa = _minutos(p.get("hora_evento"))
    if festa is not None and ev:
        saida, volta = _minutos(p.get("hora_retirada")), _minutos(p.get("hora_devolucao"))
        if ret == ev and saida is not None and saida > festa:
            alertas.append(("conflito", "Saída marcada depois do horário do evento."))
        if dev == ev and volta is not None and volta < festa:
            alertas.append(("conflito", "Devolução marcada antes do horário do evento."))
    return alertas


def _pedidos_da_agenda(conn, inicio: str, fim: str) -> list:
    """Pedidos com alguma data no período (cada data por índice próprio)."""
    rows = conn.execute(
        "SELECT p.id, p.cliente_id, p.data_evento, p.data_retirada, p.data_devolucao,"
        " p.hora_retirada, p.hora_devolucao, p.hora_evento, p.local_evento, p.status_comercial,"
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
        "hora_evento": p.get("hora_evento") or "",
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
        datas = [("historico", p.get("data_evento"), base["hora_evento"])]
    else:
        datas = [("evento", p.get("data_evento"), base["hora_evento"]),
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
        "data_devolucao": None, "hora_retirada": "", "hora_devolucao": "", "hora_evento": "",
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
    # festa sem horário e históricos primeiro (o dia todo), depois por horário;
    # compromissos da operação sem horário no fim
    dia_todo = c["tipo"] in ("evento", "historico") and not c["hora"]
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


# ---------------------------------------------------------------------------
# Login, recuperação de senha e personalização do login
#
# A autenticação continua a do sistema (usuarios + membros); aqui ficam a
# identificação por login ou e-mail, o limite de tentativas, os links de
# redefinição de senha (token guardado só como hash, uso único, 1 hora) e as
# configurações visuais do login, que vivem na tabela central configuracoes
# (logo = logo da empresa; cores primária/secundária = identidade visual).
# ---------------------------------------------------------------------------

LIMITE_FALHAS_LOGIN = 5          # por usuário digitado, na janela abaixo
LIMITE_FALHAS_IP = 20            # por endereço, na janela abaixo
JANELA_FALHAS_MINUTOS = 15
VALIDADE_LINK_SENHA_MINUTOS = 60
INTERVALO_PEDIDO_SENHA_MINUTOS = 10
SENHA_MINIMA = 8

LAYOUTS_LOGIN = {"dividido": "Dividido", "centralizado": "Centralizado"}
# Modelos prontos: aplicar um modelo só preenche os campos da tela; nada é
# gravado até "Salvar alterações".
MODELOS_LOGIN = {
    "padrao": {"rotulo": "Padrão", "login_layout": "dividido", "cor_primaria": "#6F1C85",
               "cor_secundaria": "#FF8C00", "login_cor_botao": "#6F1C85",
               "login_cor_fundo": "#FFFFFF", "login_cor_texto": "#1E1C22",
               "login_cor_texto_sec": "#6B6472"},
    "minimalista": {"rotulo": "Minimalista", "login_layout": "centralizado",
                    "cor_primaria": "#6F1C85", "cor_secundaria": "#C9B6DC",
                    "login_cor_botao": "#1E1C22", "login_cor_fundo": "#FFFFFF",
                    "login_cor_texto": "#1E1C22", "login_cor_texto_sec": "#71717A"},
    "escuro": {"rotulo": "Escuro", "login_layout": "dividido", "cor_primaria": "#A66BD6",
               "cor_secundaria": "#FF8C00", "login_cor_botao": "#8E44C4",
               "login_cor_fundo": "#17111F", "login_cor_texto": "#F4EFFA",
               "login_cor_texto_sec": "#B8AEC6"},
    "foto": {"rotulo": "Foto real", "login_layout": "dividido", "cor_primaria": "#6F1C85",
             "cor_secundaria": "#FF8C00", "login_cor_botao": "#6F1C85",
             "login_cor_fundo": "#FFFFFF", "login_cor_texto": "#1E1C22",
             "login_cor_texto_sec": "#6B6472"},
    "clean": {"rotulo": "Clean", "login_layout": "centralizado", "cor_primaria": "#6F1C85",
              "cor_secundaria": "#FFB866", "login_cor_botao": "#6F1C85",
              "login_cor_fundo": "#FFFFFF", "login_cor_texto": "#111827",
              "login_cor_texto_sec": "#6B7280"},
}
CORES_LOGIN = ("cor_primaria", "cor_secundaria", "login_cor_botao", "login_cor_fundo",
               "login_cor_texto", "login_cor_texto_sec")
# texto -> tamanho máximo
TEXTOS_LOGIN = {"login_titulo": 60, "login_subtitulo": 160, "login_slogan": 60,
                "login_mensagem": 200}
CHAVES_LOGIN = ("login_layout", "login_modelo", "login_imagem", *CORES_LOGIN,
                *TEXTOS_LOGIN)
LOGO_OFICIAL = "logo-morumbi.webp"


def _migrar_login(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tentativas_login (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chave TEXT NOT NULL,
            criado_em TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_tentativas_login ON tentativas_login (chave, criado_em);
        CREATE TABLE IF NOT EXISTS redefinicoes_senha (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
            tenant_id INTEGER REFERENCES organizacoes(id),
            token_hash TEXT NOT NULL UNIQUE,
            origem TEXT NOT NULL,
            criado_por INTEGER REFERENCES usuarios(id),
            criado_em TEXT NOT NULL,
            expira_em TEXT NOT NULL,
            usado_em TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_redefinicoes_usuario
            ON redefinicoes_senha (usuario_id, criado_em);
    """)


def _minutos_atras(minutos: int) -> str:
    agora_ = datetime.fromisoformat(formato.agora())
    return (agora_ - timedelta(minutes=minutos)).isoformat(timespec="seconds")


def usuario_para_entrar(identificacao: str) -> dict | None:
    """Usuário pelo login ou, se não houver, pelo e-mail (só se for único)."""
    ident = (identificacao or "").strip().lower()
    if not ident:
        return None
    with conectar() as conn:
        r = conn.execute("SELECT * FROM usuarios WHERE login = ?", (ident,)).fetchone()
        if r:
            return dict(r)
        if "@" in ident:
            rows = conn.execute("SELECT * FROM usuarios WHERE lower(email) = ?",
                                (ident,)).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
    return None


def _chaves_tentativa(identificacao: str, ip: str) -> tuple:
    ident = (identificacao or "").strip().lower()
    return f"u:{hashlib.sha256(ident.encode()).hexdigest()}", f"ip:{ip or '-'}"


def login_bloqueado(identificacao: str, ip: str) -> bool:
    """Muitas senhas erradas seguidas para este usuário ou deste endereço."""
    por_usuario, por_ip = _chaves_tentativa(identificacao, ip)
    desde = _minutos_atras(JANELA_FALHAS_MINUTOS)
    with conectar() as conn:
        def falhas(chave):
            return conn.execute("SELECT COUNT(*) FROM tentativas_login"
                                " WHERE chave = ? AND criado_em >= ?",
                                (chave, desde)).fetchone()[0]
        return (falhas(por_usuario) >= LIMITE_FALHAS_LOGIN
                or falhas(por_ip) >= LIMITE_FALHAS_IP)


def registrar_falha_login(identificacao: str, ip: str):
    agora_ = formato.agora()
    with conectar() as conn:
        conn.executemany("INSERT INTO tentativas_login (chave, criado_em) VALUES (?, ?)",
                         [(c, agora_) for c in _chaves_tentativa(identificacao, ip)])
        conn.execute("DELETE FROM tentativas_login WHERE criado_em < ?",
                     (_minutos_atras(24 * 60),))


def limpar_falhas_login(identificacao: str):
    with conectar() as conn:
        conn.execute("DELETE FROM tentativas_login WHERE chave = ?",
                     (_chaves_tentativa(identificacao, "")[0],))


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _criar_token(conn, usuario_id: int, tenant_id, origem: str, criado_por=None) -> str:
    token = secrets.token_urlsafe(32)
    agora_ = formato.agora()
    expira = (datetime.fromisoformat(agora_)
              + timedelta(minutes=VALIDADE_LINK_SENHA_MINUTOS)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO redefinicoes_senha (usuario_id, tenant_id, token_hash, origem,"
                 " criado_por, criado_em, expira_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (usuario_id, tenant_id, _hash_token(token), origem, criado_por,
                  agora_, expira))
    return token


def pedir_nova_senha(identificacao: str) -> tuple | None:
    """Pedido feito na tela "Esqueceu a senha?".

    Devolve (usuário, token) quando há uma conta ativa com esse login/e-mail
    e ela não pediu há poucos minutos; senão None. Quem chama nunca revela a
    diferença para a tela. O pedido fica visível para os administradores.
    """
    u = usuario_para_entrar(identificacao)
    if not u or not u["ativo"]:
        return None
    vinculos = membros_do_usuario(u["id"])
    if not vinculos:
        return None
    tid = vinculos[0]["tenant_id"]
    with conectar() as conn:
        recente = conn.execute(
            "SELECT 1 FROM redefinicoes_senha WHERE usuario_id = ? AND origem = 'pedido'"
            " AND criado_em >= ?", (u["id"], _minutos_atras(INTERVALO_PEDIDO_SENHA_MINUTOS))
        ).fetchone()
        if recente:
            return None
        token = _criar_token(conn, u["id"], tid, "pedido")
    with usando_tenant(tid):
        with conectar() as conn:
            auditar(conn, "usuario", u["id"], "pedir_senha",
                    f"{u['nome']} pediu uma nova senha", u["id"])
    return u, token


def pedidos_de_senha_pendentes() -> list:
    """Pedidos de nova senha da empresa ainda sem senha redefinida depois deles."""
    with conectar() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT r.usuario_id, MAX(r.criado_em) AS pedido_em, u.nome, u.login, u.email"
            " FROM redefinicoes_senha r JOIN usuarios u ON u.id = r.usuario_id"
            f" WHERE r.origem = 'pedido' AND {_t('r')} AND r.criado_em >= ?"
            " AND NOT EXISTS (SELECT 1 FROM redefinicoes_senha x WHERE"
            "   x.usuario_id = r.usuario_id AND x.usado_em IS NOT NULL"
            "   AND x.usado_em >= r.criado_em)"
            " GROUP BY r.usuario_id ORDER BY pedido_em DESC",
            (_minutos_atras(7 * 24 * 60),)).fetchall()]


def gerar_link_senha(usuario_id: int, admin_id=None) -> str:
    """Link de nova senha criado pelo administrador (usuário da própria empresa)."""
    u = buscar_usuario(usuario_id)
    if not u:
        raise ValueError("Usuário não encontrado.")
    if not u["ativo"]:
        raise ValueError("Ative o usuário antes de gerar um link de nova senha.")
    with conectar() as conn:
        token = _criar_token(conn, usuario_id, tenant_atual(), "admin", admin_id)
        auditar(conn, "usuario", usuario_id, "gerar_link_senha",
                f"Link de nova senha gerado para {u['nome']}", admin_id)
    return token


def link_de_senha_valido(token: str) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT r.*, u.nome, u.login, u.ativo FROM redefinicoes_senha r"
            " JOIN usuarios u ON u.id = r.usuario_id WHERE r.token_hash = ?",
            (_hash_token(token),)).fetchone()
    if not r or r["usado_em"] or r["expira_em"] < formato.agora() or not r["ativo"]:
        return None
    return dict(r)


def redefinir_senha(token: str, nova: str, confirmacao: str):
    """Troca a senha pelo link; o link (e os outros abertos) deixam de valer."""
    from werkzeug.security import generate_password_hash
    pedido = link_de_senha_valido(token)
    if not pedido:
        raise ValueError("Este link não é mais válido. Peça um novo.")
    if len(nova or "") < SENHA_MINIMA:
        raise ErroDeCampo("senha", f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.")
    if nova != confirmacao:
        raise ErroDeCampo("confirmacao", "As senhas não conferem.")
    agora_ = formato.agora()
    with conectar() as conn:
        conn.execute("UPDATE usuarios SET senha_hash = ?, atualizado_em = ? WHERE id = ?",
                     (generate_password_hash(nova), agora_, pedido["usuario_id"]))
        conn.execute("UPDATE redefinicoes_senha SET usado_em = ?"
                     " WHERE usuario_id = ? AND usado_em IS NULL",
                     (agora_, pedido["usuario_id"]))
    with usando_tenant(pedido["tenant_id"] or tenant_padrao()):
        with conectar() as conn:
            auditar(conn, "usuario", pedido["usuario_id"], "redefinir_senha",
                    f"Senha de {pedido['nome']} redefinida pelo link", pedido["usuario_id"])
    limpar_falhas_login(pedido["login"])
    return pedido


# --- Personalização do login -------------------------------------------------

def _cor_valida(valor: str) -> bool:
    return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", valor or ""))


def limpar_texto(texto: str, maximo: int) -> str:
    """Texto simples: sem marcação HTML, sem caracteres de controle, 1 linha."""
    t = re.sub(r"<[^>]*>", "", str(texto or ""))
    t = "".join(c for c in t if c.isprintable())
    return " ".join(t.replace("<", "").replace(">", "").split())[:maximo]


def _contraste(cor_a: str, cor_b: str) -> float:
    def lum(cor):
        canais = [int(cor[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in canais]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    a, b = sorted((lum(cor_a), lum(cor_b)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def _arquivo_da_empresa(caminho: str) -> bool:
    """Só arquivos gravados pelo sistema na pasta da empresa (nomes gerados)."""
    return bool(re.fullmatch(r"uploads/empresas/\d+/[A-Za-z0-9_.-]+\.(png|jpe?g|webp|svg)",
                             caminho or ""))


def config_login() -> dict:
    """Tudo o que a página de login precisa, numa leitura (cache da empresa).

    Valor ausente ou inválido volta ao padrão Morumbi Festas: o login nunca
    fica quebrado por causa de uma configuração.
    """
    c = {chave: config(chave) for chave in CHAVES_LOGIN}
    for chave in CORES_LOGIN:
        if not _cor_valida(c[chave]):
            c[chave] = CONFIG_PADRAO[chave] or CONFIG_PADRAO["cor_primaria"]
    if c["login_layout"] not in LAYOUTS_LOGIN:
        c["login_layout"] = CONFIG_PADRAO["login_layout"]
    if c["login_modelo"] not in MODELOS_LOGIN:
        c["login_modelo"] = CONFIG_PADRAO["login_modelo"]
    for chave, maximo in TEXTOS_LOGIN.items():
        c[chave] = limpar_texto(c[chave], maximo) or CONFIG_PADRAO[chave]
    if not _arquivo_da_empresa(c["login_imagem"]):
        c["login_imagem"] = ""
    logo = empresa_atual().get("logo") or ""
    c["logo"] = logo if _arquivo_da_empresa(logo) else ""
    c["logo_padrao"] = LOGO_OFICIAL
    c["texto_botao"] = "#FFFFFF" if _contraste(c["login_cor_botao"], "#FFFFFF") >= 3 else "#1E1C22"
    return c


def salvar_config_login(valores: dict, usuario_id=None) -> dict:
    """Valida e grava a personalização (auditoria: antes e depois de cada campo)."""
    limpos = {}
    for chave in CORES_LOGIN:
        if chave in valores:
            v = (valores[chave] or "").strip()
            if not _cor_valida(v):
                raise ErroDeCampo(chave, "Cor inválida. Use o formato #RRGGBB.")
            limpos[chave] = v.upper()
    if "login_layout" in valores:
        if valores["login_layout"] not in LAYOUTS_LOGIN:
            raise ErroDeCampo("login_layout", "Layout inválido.")
        limpos["login_layout"] = valores["login_layout"]
    if "login_modelo" in valores:
        if valores["login_modelo"] not in MODELOS_LOGIN:
            raise ErroDeCampo("login_modelo", "Modelo inválido.")
        limpos["login_modelo"] = valores["login_modelo"]
    for chave, maximo in TEXTOS_LOGIN.items():
        if chave in valores:
            texto = limpar_texto(valores[chave], maximo)
            if not texto and chave in ("login_titulo", "login_subtitulo"):
                raise ErroDeCampo(chave, "Preencha este texto.")
            limpos[chave] = texto
    if "login_imagem" in valores:
        limpos["login_imagem"] = valores["login_imagem"] or ""
    return salvar_config(limpos, usuario_id)


def restaurar_login(usuario_id=None) -> dict:
    """Volta o login ao padrão: apaga as configurações (valem os padrões) e a logo."""
    tid = tenant_atual()
    antes = {c: config(c) for c in CHAVES_LOGIN}
    logo = empresa_atual().get("logo") or ""
    with conectar() as conn:
        marcas = ",".join("?" * len(CHAVES_LOGIN))
        conn.execute(f"DELETE FROM configuracoes WHERE tenant_id = ? AND chave IN ({marcas})",
                     (tid, *CHAVES_LOGIN))
        conn.execute("UPDATE organizacoes SET logo = '', atualizado_em = ? WHERE id = ?",
                     (formato.agora(), tid))
        mudancas = {c: [v, CONFIG_PADRAO.get(c, "")] for c, v in antes.items()
                    if v != CONFIG_PADRAO.get(c, "")}
        if logo:
            mudancas["logo"] = [logo, ""]
        auditar(conn, "configuracao", tid, "restaurar",
                "Login restaurado para o padrão", usuario_id, mudancas)
    _config_cache.pop((CAMINHO_BD, tid), None)
    return mudancas


# ---------------------------------------------------------------------------
# Sprint 7 — Catálogo, vitrine e disponibilidade
#
# Uma regra só para estoque: quantidade física do produto menos o pico de
# unidades ocupadas, dia a dia, por pedidos em andamento no período. Kits não
# têm estoque próprio: ocupam os seus componentes (quantidade do kit × do
# componente). Históricos e cancelados não ocupam nada. Pedido sem retirada ou
# devolução ocupa pela data da festa. A vitrine, o calendário, a Agenda e a
# gravação do pedido usam as mesmas funções.
# ---------------------------------------------------------------------------

TIPOS_CATALOGO = ("produto", "kit")
SELOS = ("", "Mais popular", "Novidade", "Destaque", "Últimas unidades")
LIMIAR_ULTIMAS = 0.2          # até 20% da capacidade (mínimo 1) = últimas unidades
DIAS_USO_PADRAO = 2
SLUGS_RESERVADOS = {"kits", "pecas", "orcamento", "produto", "kit", "busca", "categoria",
                    "categorias", "disponibilidade", "api", "static", "enviado"}
ROTULO_SITUACAO = {"disponivel": "Disponível", "ultimas": "Últimas unidades",
                   "indisponivel": "Indisponível"}

# data vazia ('') vale como sem data: pedido importado ou gravado sem retirada
_R_PEDIDO = "COALESCE(NULLIF(p.data_retirada, ''), NULLIF(p.data_evento, ''))"
_D_PEDIDO = ("COALESCE(NULLIF(p.data_devolucao, ''), NULLIF(p.data_evento, ''),"
             " NULLIF(p.data_retirada, ''))")


def _migrar_catalogo(conn):
    def colunas(tabela, novas):
        existentes = _colunas(conn, tabela)
        for coluna, tipo in novas:
            if coluna not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
    regras = (("slug", "TEXT"), ("selo", "TEXT NOT NULL DEFAULT ''"),
              ("destaque", "INTEGER NOT NULL DEFAULT 0"),
              ("dias_uso", f"INTEGER NOT NULL DEFAULT {DIAS_USO_PADRAO}"),
              ("dias_antecedencia", "INTEGER NOT NULL DEFAULT 0"),
              ("seo_titulo", "TEXT NOT NULL DEFAULT ''"),
              ("seo_descricao", "TEXT NOT NULL DEFAULT ''"))
    colunas("produtos", (*regras, ("reuso_mesmo_dia", "INTEGER NOT NULL DEFAULT 0")))
    colunas("kits", (*regras, ("categoria_id", "INTEGER REFERENCES categorias(id)")))
    colunas("categorias", (("slug", "TEXT"), ("imagem", "TEXT NOT NULL DEFAULT ''"),
                           ("descricao", "TEXT NOT NULL DEFAULT ''"),
                           ("visivel", "INTEGER NOT NULL DEFAULT 1")))
    for t in ("fotos_produto", "fotos_kit"):
        colunas(t, (("miniatura", "TEXT NOT NULL DEFAULT ''"),
                    ("ordem", "INTEGER NOT NULL DEFAULT 0")))
    colunas("orcamentos", (("data_evento", "TEXT"), ("origem", "TEXT NOT NULL DEFAULT ''")))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tags_kit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kit_id INTEGER NOT NULL REFERENCES kits(id),
            tag TEXT NOT NULL,
            UNIQUE(kit_id, tag)
        );
        CREATE INDEX IF NOT EXISTS ix_itens_pedido_item ON itens_pedido (tipo, item_id);
        CREATE INDEX IF NOT EXISTS ix_itens_kit_produto ON itens_kit (produto_id);
        CREATE INDEX IF NOT EXISTS ix_pedidos_tenant_status_op ON pedidos (tenant_id, status_operacional);
    """)
    # endereços amigáveis para o que já existe (nomes e ids não mudam)
    for tabela in ("categorias", "kits", "produtos"):
        for r in conn.execute(f"SELECT id, tenant_id, nome FROM {tabela}"
                              " WHERE slug IS NULL OR slug = '' ORDER BY id").fetchall():
            conn.execute(f"UPDATE {tabela} SET slug = ? WHERE id = ?",
                         (_slug_livre(conn, r["tenant_id"], r["nome"], tabela, r["id"]), r["id"]))
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{tabela}_slug"
                     f" ON {tabela} (tenant_id, slug)")


# ---------------------------------------------------------------------------
# Sprint 5.1 — tipos de item, unidades, materiais e kits mistos.
# Só acrescenta colunas e tabelas: nada é apagado, renomeado ou reclassificado.
# Produtos já existentes ficam com tipo vazio ("a classificar") e continuam
# funcionando como locação até alguém confirmar o tipo na administração.
# ---------------------------------------------------------------------------

_TABELAS_CONFERIDAS_51 = ("produtos", "kits", "itens_kit", "pedidos", "itens_pedido",
                          "orcamentos", "itens_orcamento")


def _retrato_51(conn) -> dict:
    """Contagem e somas que precisam ser iguais antes e depois da migração."""
    r = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
         for t in _TABELAS_CONFERIDAS_51}
    r["soma_precos_produtos"] = conn.execute(
        "SELECT ROUND(COALESCE(SUM(preco_locacao), 0), 2) FROM produtos").fetchone()[0]
    r["soma_precos_kits"] = conn.execute(
        "SELECT ROUND(COALESCE(SUM(preco), 0), 2) FROM kits").fetchone()[0]
    for t in ("itens_pedido", "itens_orcamento"):
        r[f"valor_{t}"] = conn.execute(
            f"SELECT ROUND(COALESCE(SUM(quantidade * preco_unitario), 0), 2) FROM {t}").fetchone()[0]
    r["qtd_itens_kit"] = conn.execute(
        "SELECT COALESCE(SUM(quantidade), 0) FROM itens_kit").fetchone()[0]
    return r


def _precisa_migrar_51(conn) -> bool:
    return "tipo" not in _colunas(conn, "produtos")


def backup_antes_sprint51():
    """Cópia do banco antes da migração 5.1 (só uma vez e só se houver dados)."""
    if not os.path.exists(CAMINHO_BD):
        return None
    with conectar() as conn:
        tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "produtos" not in tabelas or not _precisa_migrar_51(conn):
            return None
        if not any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                   for t in ("produtos", "kits", "pedidos", "orcamentos")):
            return None
        destino = f"{CAMINHO_BD}.backup-antes-sprint51-{time.strftime('%Y%m%d-%H%M%S')}"
        n = 1
        while os.path.exists(destino):
            n += 1
            destino = f"{CAMINHO_BD}.backup-antes-sprint51-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        copia = sqlite3.connect(destino)
        try:
            conn.backup(copia)  # cópia consistente mesmo com o modo WAL
            copia.execute("PRAGMA journal_mode=DELETE")  # arquivo único, fácil de copiar
        finally:
            copia.close()
    return destino


def _migrar_sprint51(conn):
    def colunas(tabela, novas):
        existentes = _colunas(conn, tabela)
        for coluna, tipo in novas:
            if coluna not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
    primeira_vez = _precisa_migrar_51(conn)
    antes = _retrato_51(conn) if primeira_vez else None
    colunas("produtos", (
        ("tipo", "TEXT"),                       # NULL = a classificar
        ("unidade", "TEXT NOT NULL DEFAULT 'unidade'"),
        ("qtd_minima", "REAL"),
        ("prazo_producao_dias", "INTEGER NOT NULL DEFAULT 0"),
        ("tempo_producao_min", "INTEGER"),
        ("tempo_execucao_min", "INTEGER"),
        ("permite_retirada", "INTEGER NOT NULL DEFAULT 1"),
        ("permite_montagem", "INTEGER NOT NULL DEFAULT 1"),
        ("exige_agendamento", "INTEGER NOT NULL DEFAULT 0"),
        ("cobra_deslocamento", "INTEGER NOT NULL DEFAULT 0"),
        ("local_execucao", "TEXT NOT NULL DEFAULT ''"),
        ("servico_id", "INTEGER REFERENCES servicos(id)"),
    ))
    colunas("kits", (
        ("codigo_sku", "TEXT"),
        ("modo_preco", "TEXT NOT NULL DEFAULT 'fechado'"),
        ("permite_retirada", "INTEGER NOT NULL DEFAULT 1"),
        ("permite_montagem", "INTEGER NOT NULL DEFAULT 1"),
    ))
    colunas("itens_kit", (("obrigatorio", "INTEGER NOT NULL DEFAULT 1"),))
    for t in ("pedidos", "orcamentos"):
        colunas(t, (("modalidade", "TEXT NOT NULL DEFAULT ''"),))
    for t in ("itens_pedido", "itens_orcamento"):
        colunas(t, (("unidade", "TEXT NOT NULL DEFAULT ''"),
                    ("composicao", "TEXT NOT NULL DEFAULT ''")))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS materiais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES organizacoes(id),
            codigo TEXT NOT NULL DEFAULT '',
            nome TEXT NOT NULL,
            unidade TEXT NOT NULL DEFAULT 'un',
            custo_referencia REAL,
            arredondamento TEXT NOT NULL DEFAULT 'exato',
            ativo INTEGER NOT NULL DEFAULT 1,
            observacoes TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT (datetime('now')),
            atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_materiais_nome ON materiais (tenant_id, nome);
        CREATE TABLE IF NOT EXISTS receitas_produto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id INTEGER NOT NULL REFERENCES produtos(id),
            material_id INTEGER NOT NULL REFERENCES materiais(id),
            quantidade REAL NOT NULL,
            observacao TEXT NOT NULL DEFAULT '',
            UNIQUE (produto_id, material_id)
        );
        CREATE INDEX IF NOT EXISTS ix_receitas_material ON receitas_produto (material_id);
        CREATE TABLE IF NOT EXISTS movimentos_material (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES organizacoes(id),
            material_id INTEGER NOT NULL REFERENCES materiais(id),
            tipo TEXT NOT NULL,
            quantidade REAL NOT NULL,
            pedido_id INTEGER REFERENCES pedidos(id),
            item_pedido_id INTEGER REFERENCES itens_pedido(id),
            usuario_id INTEGER REFERENCES usuarios(id),
            observacao TEXT NOT NULL DEFAULT '',
            criado_em TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS ix_mov_material ON movimentos_material (tenant_id, material_id);
        CREATE INDEX IF NOT EXISTS ix_mov_pedido ON movimentos_material (pedido_id);
        CREATE INDEX IF NOT EXISTS ix_produtos_tipo ON produtos (tenant_id, tipo);
    """)
    if primeira_vez:
        depois = _retrato_51(conn)
        if depois != antes:  # para a inicialização; o backup feito antes fica para restaurar
            raise RuntimeError(f"Migração 5.1 interrompida: dados mudaram {antes} -> {depois}")
        if antes["produtos"] or antes["pedidos"]:
            conn.execute(
                "INSERT INTO audit_log (tenant_id, usuario_id, tipo, entidade, entidade_id,"
                " descricao, dados, criado_em) VALUES (?, NULL, 'migracao_51', 'sistema', NULL,"
                " ?, ?, ?)",
                (conn.execute("SELECT MIN(id) FROM organizacoes").fetchone()[0] or 1,
                 "Migração Sprint 5.1: tipos de item, unidades e materiais",
                 json.dumps({"antes": antes, "depois": depois,
                             "a_classificar": antes["produtos"]}, ensure_ascii=False),
                 formato.agora()))


def slugificar(texto: str) -> str:
    t = normalizar_texto(texto)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:80].strip("-") or "item"


def _slug_livre(conn, tenant_id, nome: str, tabela: str, id_=None, desejado: str = "") -> str:
    """Slug único na empresa entre categorias, kits e produtos (mesmo endereço)."""
    base = slugificar(desejado or nome)
    if base in SLUGS_RESERVADOS:
        sufixo = {"produtos": "item", "kits": "kit", "categorias": "tema"}[tabela]
        base = f"{base}-{sufixo}"
    candidato, n = base, 1
    while True:
        usado = False
        for t in ("categorias", "kits", "produtos"):
            r = conn.execute(f"SELECT id FROM {t} WHERE tenant_id = ? AND slug = ?",
                             (tenant_id, candidato)).fetchone()
            if r and not (t == tabela and r["id"] == id_):
                usado = True
                break
        if not usado:
            return candidato
        n += 1
        candidato = f"{base}-{n}"


# --- Motor de disponibilidade -------------------------------------------------

def janela_da_festa(data_festa: str, dias_uso: int) -> tuple:
    """Período em que o item fica fora para uma festa.

    1 dia = só o dia da festa; 2 = véspera (retirada) e festa; 3 = véspera,
    festa e dia seguinte (devolução); e assim por diante.
    """
    d = date.fromisoformat(data_festa)
    dias = max(1, int(dias_uso or 1))
    inicio = d if dias == 1 else d - timedelta(days=1)
    fim = max(d, inicio + timedelta(days=dias - 1))
    return inicio.isoformat(), fim.isoformat()


def _ocupacoes(conn, produto_ids, inicio: str, fim: str, excluir_pedido=None) -> list:
    """Unidades de cada produto ocupadas por pedidos em andamento no período."""
    ids = sorted({int(i) for i in produto_ids if i})
    if not ids:
        return []
    marcas = ",".join("?" * len(ids))
    filtro = (f" AND {_em_operacao('p')} AND p.historico = 0"
              " AND p.status_operacional NOT IN ('cancelado', 'finalizado')"
              f" AND {_R_PEDIDO} IS NOT NULL AND {_R_PEDIDO} <= ? AND {_D_PEDIDO} >= ?"
              + (" AND p.id != ?" if excluir_pedido else ""))
    extra = [fim, inicio] + ([excluir_pedido] if excluir_pedido else [])
    # Kits: a composição gravada na venda (Sprint 5.1); pedidos anteriores,
    # sem retrato, usam a composição atual. Só peças obrigatórias de locação.
    j = "json_extract(j.value, '$.{}')"
    sql = (f"SELECT ip.item_id AS produto_id, ip.quantidade AS qtd, {_R_PEDIDO} AS r,"
           f" {_D_PEDIDO} AS d, p.id AS pedido_id"
           " FROM itens_pedido ip JOIN pedidos p ON p.id = ip.pedido_id"
           f" WHERE ip.tipo = 'produto' AND ip.item_id IN ({marcas}){filtro}"
           f" UNION ALL SELECT ik.produto_id, ip.quantidade * ik.quantidade, {_R_PEDIDO},"
           f" {_D_PEDIDO}, p.id"
           " FROM itens_pedido ip JOIN itens_kit ik ON ik.kit_id = ip.item_id"
           " JOIN pedidos p ON p.id = ip.pedido_id"
           f" WHERE ip.tipo = 'kit' AND COALESCE(ip.composicao, '') = ''"
           f" AND ik.obrigatorio = 1 AND ik.produto_id IN ({marcas}){filtro}"
           f" UNION ALL SELECT {j.format('produto_id')}, ip.quantidade * {j.format('quantidade')},"
           f" {_R_PEDIDO}, {_D_PEDIDO}, p.id"
           " FROM itens_pedido ip JOIN pedidos p ON p.id = ip.pedido_id,"
           " json_each(ip.composicao) j"
           f" WHERE ip.tipo = 'kit' AND COALESCE(ip.composicao, '') != ''"
           f" AND {j.format('estoque')} = 1 AND {j.format('obrigatorio')} = 1"
           f" AND {j.format('produto_id')} IN ({marcas}){filtro}")
    return [dict(r) for r in conn.execute(sql, [*ids, *extra, *ids, *extra, *ids, *extra]).fetchall()]


def _dias(inicio: str, fim: str):
    d, f = date.fromisoformat(inicio), date.fromisoformat(fim)
    while d <= f:
        yield d.isoformat()
        d += timedelta(days=1)


def _pico(ocupacoes: list, inicio: str, fim: str, reuso: bool = False) -> int:
    """Maior quantidade ocupada ao mesmo tempo em algum dia do período.

    Com reuso no mesmo dia, o item que volta de manhã pode sair à tarde: o dia
    da devolução de um pedido não conflita com o dia da retirada do outro.
    """
    pico = 0
    for dia in _dias(inicio, fim):
        soma = 0
        for o in ocupacoes:
            if not (o["r"] <= dia <= o["d"]):
                continue
            if reuso and ((dia == inicio and o["d"] == dia and o["r"] < dia)
                          or (dia == fim and o["r"] == dia and o["d"] > dia)):
                continue
            soma += o["qtd"]
        pico = max(pico, soma)
    return pico


def _produtos_info(conn, ids) -> dict:
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    marcas = ",".join("?" * len(ids))
    return {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, nome, quantidade_total, status, reuso_mesmo_dia FROM produtos"
        f" WHERE id IN ({marcas}) AND {_t()}", ids).fetchall()}


def livres_no_periodo(conn, produto_ids, inicio: str, fim: str, excluir_pedido=None) -> dict:
    """{produto_id: unidades livres} no período (0 em manutenção ou inativo)."""
    info = _produtos_info(conn, produto_ids)
    ocup = _ocupacoes(conn, info.keys(), inicio, fim, excluir_pedido)
    livres = {}
    for pid, p in info.items():
        if p["status"] != "disponivel":
            livres[pid] = 0
            continue
        usados = _pico([o for o in ocup if o["produto_id"] == pid], inicio, fim,
                       bool(p["reuso_mesmo_dia"]))
        livres[pid] = max(p["quantidade_total"] - usados, 0)
    return livres


def _necessidades(conn, itens: list) -> dict:
    """Unidades físicas de locação pedidas por produto (kits abertos nas peças
    obrigatórias). Encomenda e serviço não ocupam estoque físico."""
    precisa: dict = {}
    for item in itens:
        if not item.get("item_id"):
            continue
        qtd = regras.numero(item.get("quantidade") or 1)
        if item.get("tipo") == "produto":
            precisa[int(item["item_id"])] = precisa.get(int(item["item_id"]), 0) + qtd
        elif item.get("tipo") == "kit":
            comps = _ler_composicao(item.get("composicao")) or _composicao_venda(conn, item["item_id"])
            for c in comps:
                if c.get("obrigatorio", 1) and c.get("estoque", 1):
                    pid = int(c["produto_id"])
                    precisa[pid] = precisa.get(pid, 0) + qtd * float(c["quantidade"])
    if precisa:
        marcas = ",".join("?" * len(precisa))
        tipos = {r["id"]: r["tipo"] for r in conn.execute(
            f"SELECT id, tipo FROM produtos WHERE id IN ({marcas})", list(precisa))}
        precisa = {pid: q for pid, q in precisa.items()
                   if pid not in tipos or regras.consome_estoque(tipos[pid])}
    return {pid: int(q) if q == int(q) else q for pid, q in precisa.items()}


def _faltas_de_estoque(conn, itens: list, data_retirada: str, data_devolucao: str,
                       pedido_id: int | None = None) -> list:
    """Produtos (inclusive dentro de kits) sem quantidade livre no período."""
    faltas = []
    for item in itens:  # encomenda e serviço: só precisam estar ativos
        if item.get("tipo") == "produto" and item.get("item_id"):
            r = conn.execute(f"SELECT nome, tipo, status FROM produtos WHERE id = ? AND {_t()}",
                             (item["item_id"],)).fetchone()
            if r and not regras.consome_estoque(r["tipo"]) and r["status"] == "inativo":
                faltas.append(f"Produto '{r['nome']}' está inativo e não pode ser vendido.")
    precisa = _necessidades(conn, itens)
    if not precisa:
        return faltas
    info = _produtos_info(conn, precisa)
    livres = livres_no_periodo(conn, precisa, data_retirada, data_devolucao, pedido_id)
    for pid, qtd in precisa.items():
        p = info.get(pid)
        if not p:
            faltas.append("Produto não encontrado.")
        elif p["status"] == "manutencao":
            faltas.append(f"Produto '{p['nome']}' esta em manutencao.")
        elif p["status"] == "inativo":
            faltas.append(f"Produto '{p['nome']}' está inativo e não pode ser reservado.")
        elif qtd > livres[pid]:
            faltas.append(f"Quantidade insuficiente para '{p['nome']}' nesta data."
                          f" Disponivel: {livres[pid]}, solicitado: {qtd}.")
    return faltas


def disponibilidade(produto_id: int, data_inicio: str | None = None,
                    data_fim: str | None = None) -> int:
    """Unidades livres de um produto no período (sem período: o estoque físico)."""
    with conectar() as conn:
        info = _produtos_info(conn, [produto_id]).get(produto_id)
        if not info or info["status"] == "manutencao":
            return 0
        if not data_inicio or not data_fim:
            return info["quantidade_total"]
        return livres_no_periodo(conn, [produto_id], data_inicio, data_fim)[produto_id]


def _componentes(conn, kit_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT ik.id AS item_id, ik.produto_id, ik.quantidade, ik.obrigatorio, p.nome,"
        " p.codigo_sku, p.quantidade_total, p.status, p.tipo, p.unidade,"
        " p.preco_locacao AS preco, p.permite_retirada, p.permite_montagem,"
        " p.local_execucao, p.prazo_producao_dias, p.dias_antecedencia"
        f" FROM itens_kit ik JOIN produtos p ON p.id = ik.produto_id AND {_t('p')}"
        " WHERE ik.kit_id = ? ORDER BY ik.obrigatorio DESC, p.nome", (kit_id,)).fetchall()]


def disponibilidade_kit(kit_id: int, data_inicio: str | None = None,
                        data_fim: str | None = None) -> int:
    """Kits completos possíveis: o componente mais escasso limita (sem estoque próprio)."""
    with conectar() as conn:
        if not _do_tenant(conn, "kits", kit_id):
            return 0
        comps = [c for c in _componentes(conn, kit_id) if c["obrigatorio"]]
        if not comps or any(c["status"] != "disponivel" for c in comps):
            return 0
        comps = [c for c in comps if regras.consome_estoque(c["tipo"])]
        if not comps:
            return None  # sem peça de locação: estoque físico não limita
        if data_inicio and data_fim:
            livres = livres_no_periodo(conn, [c["produto_id"] for c in comps],
                                       data_inicio, data_fim)
        else:
            livres = {c["produto_id"]: c["quantidade_total"] for c in comps}
        return min(int(livres[c["produto_id"]] // max(c["quantidade"], 1)) for c in comps)


def _situacao(livres: int, capacidade: int, quantidade: int = 1) -> str:
    if livres < max(quantidade, 1) or capacidade <= 0:
        return "indisponivel"
    limiar = max(1, int(capacidade * LIMIAR_ULTIMAS + 0.999))
    return "ultimas" if livres - quantidade < limiar else "disponivel"


def _regras_item(conn, tipo: str, id_: int) -> dict | None:
    tabela = "produtos" if tipo == "produto" else "kits"
    r = conn.execute(f"SELECT * FROM {tabela} WHERE id = ? AND {_t()}", (id_,)).fetchone()
    return dict(r) if r else None


def calendario_item(tipo: str, id_: int, inicio: str, fim: str, quantidade=1,
                    hoje: str | None = None) -> list:
    """Situação de cada data de festa entre inicio e fim (uma consulta ao banco).

    Locação: estoque físico menos pedidos em andamento na janela da festa.
    Encomenda: prazo mínimo de produção (materiais e capacidade: Sprint 6).
    Serviço: só ativo e antecedência (agenda da equipe: Sprint 6).
    Kit: peças obrigatórias — a de locação mais escassa limita, e vale o maior
    prazo entre o kit e suas encomendas.
    """
    if tipo not in TIPOS_CATALOGO:
        raise ValueError("Tipo inválido.")
    hoje = hoje or _hoje_iso()
    quantidade = regras.numero(quantidade or 1)
    with conectar() as conn:
        item = _regras_item(conn, tipo, id_)
        if not item:
            return []
        dias_uso = item.get("dias_uso") or DIAS_USO_PADRAO
        margem = timedelta(days=max(dias_uso, 1) + 1)
        janela_ini = (date.fromisoformat(inicio) - margem).isoformat()
        janela_fim = (date.fromisoformat(fim) + margem).isoformat()
        prazo = int(item.get("dias_antecedencia") or 0)
        if tipo == "produto":
            comps = [{"produto_id": id_, "quantidade": 1, "status": item["status"],
                      "quantidade_total": item["quantidade_total"], "tipo": item.get("tipo"),
                      "reuso": item.get("reuso_mesmo_dia"), "obrigatorio": 1}]
            prazo = max(prazo, int(item.get("prazo_producao_dias") or 0))
            ativo = item["status"] == "disponivel"
        else:
            comps = [c for c in _componentes(conn, id_) if c["obrigatorio"]]
            info = _produtos_info(conn, [c["produto_id"] for c in comps])
            for c in comps:
                c["reuso"] = info.get(c["produto_id"], {}).get("reuso_mesmo_dia")
                if c["tipo"] == "encomenda":
                    prazo = max(prazo, int(c["prazo_producao_dias"] or 0))
            ativo = item["status"] == "ativo" and bool(comps) and all(
                c["status"] == "disponivel" for c in comps)
        estoque = [c for c in comps if regras.consome_estoque(c.get("tipo"))]
        ocup = _ocupacoes(conn, [c["produto_id"] for c in estoque], janela_ini, janela_fim)
    limite_antecedencia = (date.fromisoformat(hoje) + timedelta(days=prazo)).isoformat()
    if not ativo:
        capacidade = 0
    elif estoque:
        capacidade = min(int(c["quantidade_total"] // max(c["quantidade"], 1)) for c in estoque)
    else:
        capacidade = None  # sem peça de locação: o estoque físico não limita
    sem_estoque = ("Produzido sob encomenda." if any(c.get("tipo") == "encomenda" for c in comps)
                   else "Sujeito à agenda da equipe." if comps else "")
    dias = []
    for dia in _dias(inicio, fim):
        ini_j, fim_j = janela_da_festa(dia, dias_uso)
        motivo, livres = "", None
        if not ativo:
            livres, motivo = 0, "Item indisponível no momento."
        elif dia < hoje:
            livres, motivo = 0, "Data já passou."
        elif dia < limite_antecedencia:
            livres = 0
            motivo = (f"Reserve com pelo menos {prazo} dia{'s' if prazo != 1 else ''}"
                      " de antecedência.")
        elif estoque:
            livres = max(min(
                int((c["quantidade_total"] - _pico([o for o in ocup
                                                    if o["produto_id"] == c["produto_id"]],
                                                   ini_j, fim_j, bool(c["reuso"])))
                    // max(c["quantidade"], 1)) for c in estoque), 0)
        if motivo:
            situacao = "indisponivel"
        elif livres is None:
            situacao = "disponivel"
        else:
            situacao = _situacao(livres, capacidade, quantidade)
        dias.append({"data": dia, "situacao": situacao, "rotulo": ROTULO_SITUACAO[situacao],
                     "livres": livres, "capacidade": capacidade, "motivo": motivo,
                     "observacao": "" if motivo or estoque else sem_estoque,
                     "retirada": ini_j, "devolucao": fim_j})
    return dias


def situacao_na_data(tipo: str, id_: int, data_festa: str, quantidade: int = 1,
                     hoje: str | None = None) -> dict | None:
    date.fromisoformat(data_festa)  # ValueError em data inválida
    dias = calendario_item(tipo, id_, data_festa, data_festa, quantidade, hoje)
    return dias[0] if dias else None


def disponibilidade_calendario(produto_id: int, ano: int, mes: int) -> list:
    """Compatibilidade: unidades livres de um produto em cada dia do mês."""
    import calendar
    ultimo = calendar.monthrange(ano, mes)[1]
    inicio, fim = f"{ano}-{mes:02d}-01", f"{ano}-{mes:02d}-{ultimo:02d}"
    with conectar() as conn:
        info = _produtos_info(conn, [produto_id]).get(produto_id)
        if not info:
            return []
        ocup = _ocupacoes(conn, [produto_id], inicio, fim)
    resultado = []
    for n, dia in enumerate(_dias(inicio, fim), start=1):
        livre = 0 if info["status"] == "manutencao" else max(
            info["quantidade_total"] - _pico(ocup, dia, dia), 0)
        resultado.append({"dia": n, "total": info["quantidade_total"], "disponivel": livre})
    return resultado


# --- Cadastro de produtos e kits (validação e auditoria) ------------------------

# Campos que mexem no estoque e nas regras de reserva: só quem tem inventory.edit.
CAMPOS_ESTOQUE = ("quantidade_total", "dias_uso", "dias_antecedencia", "reuso_mesmo_dia",
                  "prazo_producao_dias")
_AUDITADOS_ITEM = ("nome", "categoria_id", "descricao", "preco_locacao", "valor_referencia",
                   "preco", "quantidade_total", "status", "publicado", "selo", "destaque",
                   "dias_uso", "dias_antecedencia", "reuso_mesmo_dia", "slug", "seo_titulo",
                   "seo_descricao", "localizacao", "observacoes", "tipo", "unidade",
                   "qtd_minima", "prazo_producao_dias", "tempo_producao_min",
                   "tempo_execucao_min", "permite_retirada", "permite_montagem",
                   "exige_agendamento", "cobra_deslocamento", "local_execucao", "servico_id",
                   "codigo_sku", "modo_preco")
# Campos do Sprint 5.1 que só existem no formulário novo (ausentes = mantém)
_CAMPOS_51_PRODUTO = ("tipo", "unidade", "qtd_minima", "prazo_producao_dias",
                      "tempo_producao_min", "tempo_execucao_min", "permite_retirada",
                      "permite_montagem", "exige_agendamento", "cobra_deslocamento",
                      "local_execucao", "servico_id")


def _texto_form(form, campo, padrao=""):
    v = form.get(campo)
    return padrao if v is None else str(v).strip()


def campos_produto(form) -> dict:
    """Valores do formulário como vieram (a validação é em salvar_produto)."""
    return {
        "nome": _texto_form(form, "nome"),
        "categoria_id": _texto_form(form, "categoria_id") or None,
        "descricao": _texto_form(form, "descricao"),
        "preco_locacao": _texto_form(form, "preco_locacao", "0") or "0",
        "valor_referencia": _texto_form(form, "valor_referencia", "0") or "0",
        "quantidade_total": _texto_form(form, "quantidade_total", "1") or "0",
        "status": form.get("status", "disponivel"),
        "publicado": 1 if form.get("publicado") else 0,
        "localizacao": _texto_form(form, "localizacao"),
        "observacoes": _texto_form(form, "observacoes"),
        "selo": _texto_form(form, "selo"),
        "destaque": 1 if form.get("destaque") else 0,
        "dias_uso": _texto_form(form, "dias_uso", str(DIAS_USO_PADRAO)) or str(DIAS_USO_PADRAO),
        "dias_antecedencia": _texto_form(form, "dias_antecedencia", "0") or "0",
        "reuso_mesmo_dia": 1 if form.get("reuso_mesmo_dia") else 0,
        "slug": _texto_form(form, "slug"),
        "seo_titulo": _texto_form(form, "seo_titulo"),
        "seo_descricao": _texto_form(form, "seo_descricao"),
        **_campos_51_form(form),
    }


def _campos_51_form(form) -> dict:
    """Campos do 5.1. O formulário novo envia 'modelo_51'; sem ele (clientes
    antigos), nada disso é alterado."""
    if not form.get("modelo_51"):
        return {}
    return {
        "tipo": form.get("tipo") or "",
        "unidade": form.get("unidade") or "",
        "qtd_minima": _texto_form(form, "qtd_minima"),
        "prazo_producao_dias": _texto_form(form, "prazo_producao_dias", "0") or "0",
        "tempo_producao_min": _texto_form(form, "tempo_producao_min"),
        "tempo_execucao_min": _texto_form(form, "tempo_execucao_min"),
        "permite_retirada": 1 if form.get("permite_retirada") else 0,
        "permite_montagem": 1 if form.get("permite_montagem") else 0,
        "exige_agendamento": 1 if form.get("exige_agendamento") else 0,
        "cobra_deslocamento": 1 if form.get("cobra_deslocamento") else 0,
        "local_execucao": form.get("local_execucao") or "",
        "servico_id": _texto_form(form, "servico_id") or None,
    }


def campos_kit(form) -> dict:
    d = campos_produto(form)
    d["preco"] = _texto_form(form, "preco", "0") or "0"
    d["status"] = form.get("status", "ativo")
    for c in ("preco_locacao", "valor_referencia", "quantidade_total", "localizacao",
              "observacoes", "reuso_mesmo_dia", *_CAMPOS_51_PRODUTO):
        d.pop(c, None)
    if form.get("modelo_51"):
        d.update(codigo_sku=_texto_form(form, "codigo_sku"),
                 modo_preco=form.get("modo_preco") or "fechado",
                 permite_retirada=1 if form.get("permite_retirada") else 0,
                 permite_montagem=1 if form.get("permite_montagem") else 0)
    return d


def _numero(valor, campo: str, rotulo: str, inteiro=False, minimo=0, maximo=None):
    texto = str(valor if valor is not None else "").strip().replace(" ", "")
    if not inteiro and "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        n = int(texto) if inteiro else round(float(texto), 2)
    except (TypeError, ValueError):
        raise ErroDeCampo(campo, f"{rotulo} inválido.")
    if n != n or n in (float("inf"), float("-inf")):  # NaN/infinito
        raise ErroDeCampo(campo, f"{rotulo} inválido.")
    if n < minimo:
        raise ErroDeCampo(campo, f"{rotulo} não pode ser menor que {minimo}.")
    if maximo is not None and n > maximo:
        raise ErroDeCampo(campo, f"{rotulo} não pode ser maior que {maximo}.")
    return n


def _validar_comuns(conn, d: dict, tabela: str, id_) -> dict:
    nome = " ".join((d.get("nome") or "").split())
    if not nome:
        raise ErroDeCampo("nome", f"Nome do {'kit' if tabela == 'kits' else 'produto'} é obrigatório.")
    if len(nome) > 120:
        raise ErroDeCampo("nome", "Nome muito longo (máximo 120 caracteres).")
    cat = d.get("categoria_id")
    if cat not in (None, "", 0):
        try:
            cat = int(cat)
        except (TypeError, ValueError):
            raise ErroDeCampo("categoria_id", "Categoria não encontrada.")
        if not _do_tenant(conn, "categorias", cat):
            raise ErroDeCampo("categoria_id", "Categoria não encontrada.")
    else:
        cat = None
    selo = (d.get("selo") or "").strip()
    if selo not in SELOS:
        raise ErroDeCampo("selo", "Selo inválido.")
    slug = (d.get("slug") or "").strip().lower()
    if slug and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug):
        raise ErroDeCampo("slug", "Use só letras minúsculas, números e hífens (ex.: kit-safari).")
    if slug in SLUGS_RESERVADOS:
        raise ErroDeCampo("slug", "Este endereço é reservado. Escolha outro.")
    slug_final = _slug_livre(conn, tenant_atual(), nome, tabela, id_, slug)
    if slug and slug_final != slug:
        raise ErroDeCampo("slug", "Já existe um item ou categoria com este endereço.")
    return {
        "nome": nome, "categoria_id": cat, "descricao": (d.get("descricao") or "").strip()[:4000],
        "publicado": 1 if d.get("publicado") else 0, "selo": selo,
        "destaque": 1 if d.get("destaque") else 0, "slug": slug_final,
        "dias_uso": _numero(d.get("dias_uso", DIAS_USO_PADRAO), "dias_uso", "Dias de uso",
                            inteiro=True, minimo=1, maximo=30),
        "dias_antecedencia": _numero(d.get("dias_antecedencia", 0), "dias_antecedencia",
                                     "Antecedência mínima", inteiro=True, maximo=365),
        "seo_titulo": (d.get("seo_titulo") or "").strip()[:70],
        "seo_descricao": (d.get("seo_descricao") or "").strip()[:170],
    }


def _mudancas(antes: dict | None, depois: dict) -> dict:
    antes = antes or {}
    return {c: [antes.get(c), depois.get(c)] for c in _AUDITADOS_ITEM
            if c in depois and str(antes.get(c) if antes.get(c) is not None else "")
            != str(depois.get(c) if depois.get(c) is not None else "")}


def _gravar_tags(conn, tabela: str, coluna: str, item_id: int, tags):
    if tags is None:
        return
    conn.execute(f"DELETE FROM {tabela} WHERE {coluna} = ?", (item_id,))
    for tag in {" ".join(t.split())[:40] for t in tags if t and t.strip()}:
        conn.execute(f"INSERT OR IGNORE INTO {tabela} ({coluna}, tag) VALUES (?, ?)",
                     (item_id, tag))


def _inteiro_opcional(valor, campo, rotulo, maximo):
    if valor in (None, ""):
        return None
    return _numero(valor, campo, rotulo, inteiro=True, maximo=maximo)


def _validar_51_produto(conn, d: dict, atual: dict | None) -> dict:
    """Tipo, unidade e campos de cada tipo. Campo ausente = mantém o atual
    (ou o padrão, em item novo); 'tipo' presente e vazio é recusado."""
    base = dict(atual or {})

    def valor(c, padrao=None):
        return d[c] if c in d else base.get(c, padrao)

    if d.get("tipo") == "":  # formulário enviado sem escolher
        raise ErroDeCampo("tipo", "Escolha o tipo do item.")
    if "tipo" in d:
        tipo = d["tipo"]  # None só por código (cópia de item ainda a classificar)
    else:
        tipo = base.get("tipo") if atual else "locacao"
    if tipo not in (None, *regras.TIPOS_ITEM):
        raise ErroDeCampo("tipo", "Tipo de item inválido.")
    unidade = d.get("unidade") if d.get("unidade") else (
        base.get("unidade") if atual and base.get("unidade") in regras.UNIDADES_POR_TIPO[tipo]
        else regras.UNIDADE_PADRAO[tipo])
    unidade = regras.validar_tipo_unidade(tipo, unidade)
    v = {"tipo": tipo, "unidade": unidade}
    qtd_min = valor("qtd_minima")
    v["qtd_minima"] = (regras.quantidade(qtd_min, unidade, "qtd_minima")
                       if qtd_min not in (None, "") else None)
    v["prazo_producao_dias"] = _numero(valor("prazo_producao_dias", 0) or 0, "prazo_producao_dias",
                                       "Prazo de produção", inteiro=True, maximo=365)
    v["tempo_producao_min"] = _inteiro_opcional(valor("tempo_producao_min"), "tempo_producao_min",
                                                "Tempo de produção", 100000)
    v["tempo_execucao_min"] = _inteiro_opcional(valor("tempo_execucao_min"), "tempo_execucao_min",
                                                "Tempo de execução", 100000)
    for c in ("permite_retirada", "permite_montagem"):
        v[c] = 1 if valor(c, 1) else 0
    for c in ("exige_agendamento", "cobra_deslocamento"):
        v[c] = 1 if valor(c, 0) else 0
    local = valor("local_execucao", "") or ""
    if local and local not in regras.LOCAIS_EXECUCAO:
        raise ErroDeCampo("local_execucao", "Local de execução inválido.")
    servico_id = valor("servico_id")
    if servico_id not in (None, "", 0):
        try:
            servico_id = int(servico_id)
        except (TypeError, ValueError):
            raise ErroDeCampo("servico_id", "Atividade não encontrada.")
        if not conn.execute(f"SELECT 1 FROM servicos WHERE id = ? AND {_t()}",
                            (servico_id,)).fetchone():
            raise ErroDeCampo("servico_id", "Atividade não encontrada.")
    else:
        servico_id = None
    if tipo == "servico":
        local = local or "evento"
        if local == "evento":
            v["permite_retirada"] = 0  # serviço no evento não acontece na retirada
    else:
        local, servico_id, v["exige_agendamento"], v["cobra_deslocamento"] = "", None, 0, 0
        v["tempo_execucao_min"] = None
    if tipo != "encomenda":
        v["prazo_producao_dias"], v["tempo_producao_min"] = 0, None
    v.update(local_execucao=local, servico_id=servico_id)
    if not (v["permite_retirada"] or v["permite_montagem"]):
        raise ErroDeCampo("permite_retirada", "Escolha ao menos uma modalidade de atendimento.")
    return v


def _validar_51_kit(conn, d: dict, atual: dict | None, id_) -> dict:
    base = dict(atual or {})
    modo = d.get("modo_preco", base.get("modo_preco") or "fechado")
    if modo not in ("fechado", "componentes"):
        raise ErroDeCampo("modo_preco", "Forma de preço inválida.")
    v = {"modo_preco": modo}
    for c in ("permite_retirada", "permite_montagem"):
        v[c] = 1 if d.get(c, base.get(c, 1)) else 0
    if not (v["permite_retirada"] or v["permite_montagem"]):
        raise ErroDeCampo("permite_retirada", "Escolha ao menos uma modalidade de atendimento.")
    if id_:
        possiveis, restricoes = regras.modalidades_do_kit(v, _componentes(conn, id_))
        if _componentes(conn, id_) and not possiveis:
            raise ErroDeCampo("permite_retirada", "Os componentes não permitem as modalidades"
                                                  " marcadas. " + " ".join(restricoes))
    sku = " ".join(str(d.get("codigo_sku", base.get("codigo_sku")) or "").split()).upper()[:40]
    if not sku:
        sku = base.get("codigo_sku") or _proximo_sku_kit(conn)
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]*", sku):
        raise ErroDeCampo("codigo_sku", "Código: use letras, números, ponto, hífen ou sublinhado.")
    if conn.execute(f"SELECT 1 FROM kits WHERE codigo_sku = ? AND id != ? AND {_t()}",
                    (sku, id_ or 0)).fetchone() or conn.execute(
            "SELECT 1 FROM produtos WHERE codigo_sku = ?", (sku,)).fetchone():
        raise ErroDeCampo("codigo_sku", "Já existe um item com este código.")
    v["codigo_sku"] = sku
    return v


def _proximo_sku_kit(conn) -> str:
    n = conn.execute(f"SELECT COUNT(*) FROM kits WHERE {_t()}").fetchone()[0] + 1
    while True:
        sku = f"KIT-{n:04d}"
        if not conn.execute(f"SELECT 1 FROM kits WHERE codigo_sku = ? AND {_t()}", (sku,)).fetchone():
            return sku
        n += 1


def _sincronizar_precos_kits(conn, kit_ids=None, produto_id=None):
    """Kits com preço pelos componentes acompanham os preços atuais das peças.
    Pedidos e orçamentos já gravados não mudam: cada linha guarda o próprio preço."""
    if produto_id:
        kit_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT kit_id FROM itens_kit WHERE produto_id = ?", (produto_id,))]
    for kid in kit_ids or []:
        k = conn.execute("SELECT modo_preco, preco FROM kits WHERE id = ?", (kid,)).fetchone()
        if not k or k["modo_preco"] != "componentes":
            continue
        novo = regras.preco_final_kit("componentes", k["preco"], _componentes(conn, kid))
        if round(k["preco"] or 0, 2) != novo:
            conn.execute("UPDATE kits SET preco = ?, atualizado_em = ? WHERE id = ?",
                         (novo, formato.agora(), kid))


def salvar_produto(dados_: dict, id_: int | None = None,
                   tags: list | None = None, usuario_id=None) -> int:
    """Cria ou edita um produto físico (peça) com validação e auditoria."""
    status = dados_.get("status", "disponivel")
    if status not in STATUS_PRODUTO:
        raise ErroDeCampo("status", "Status inválido.")
    agora_ = formato.agora()
    with conectar() as conn:
        atual = None
        if id_:
            r = conn.execute(f"SELECT * FROM produtos WHERE id = ? AND {_t()}", (id_,)).fetchone()
            if not r:
                raise ValueError("Produto não encontrado.")
            atual = dict(r)
        v = _validar_comuns(conn, dados_, "produtos", id_)
        v.update(_validar_51_produto(conn, dados_, atual))
        v.update(
            status=status,
            preco_locacao=_numero(dados_.get("preco_locacao", 0), "preco_locacao", "Preço"),
            valor_referencia=_numero(dados_.get("valor_referencia", 0), "valor_referencia",
                                     "Valor de referência"),
            quantidade_total=_numero(dados_.get("quantidade_total", 1), "quantidade_total",
                                     "Quantidade", inteiro=True, maximo=100000)
            if regras.consome_estoque(v["tipo"])
            else (atual or {}).get("quantidade_total", 0),
            reuso_mesmo_dia=1 if dados_.get("reuso_mesmo_dia") else 0,
            localizacao=(dados_.get("localizacao") or "").strip()[:200],
            observacoes=(dados_.get("observacoes") or "").strip()[:2000])
        colunas = list(v)
        if atual:
            conn.execute(
                "UPDATE produtos SET " + ", ".join(f"{c} = ?" for c in colunas)
                + f", atualizado_em = ? WHERE id = ? AND {_t()}",
                (*v.values(), agora_, id_))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO produtos (tenant_id, codigo_sku, criado_em, atualizado_em, "
                + ", ".join(colunas) + ") VALUES (?, ?, ?, ?, " + ", ".join("?" * len(colunas)) + ")",
                (tenant_atual(), gerar_sku(v["categoria_id"]), agora_, agora_, *v.values()))
            novo_id = r.lastrowid
        _gravar_tags(conn, "tags_produto", "produto_id", novo_id, tags)
        _sincronizar_precos_kits(conn, produto_id=novo_id)
        mudancas = _mudancas(atual, v)
        if mudancas or not atual:
            auditar(conn, "produto", novo_id, "alterar" if atual else "criar",
                    f"Produto {v['nome']} {'alterado' if atual else 'criado'}", usuario_id,
                    mudancas if atual else None,
                    tipo=f"produto_{'alterou' if atual else 'criou'}",
                    dados={"preco": v["preco_locacao"], "quantidade": v["quantidade_total"]}
                    if not atual else None)
        return novo_id


def salvar_kit(dados_: dict, id_: int | None = None, tags: list | None = None,
               usuario_id=None) -> int:
    """Cria ou edita um kit (conjunto de produtos; sem estoque próprio)."""
    status = dados_.get("status", "ativo")
    if status not in STATUS_KIT:
        raise ErroDeCampo("status", "Status inválido.")
    agora_ = formato.agora()
    with conectar() as conn:
        atual = None
        if id_:
            r = conn.execute(f"SELECT * FROM kits WHERE id = ? AND {_t()}", (id_,)).fetchone()
            if not r:
                raise ValueError("Kit não encontrado.")
            atual = dict(r)
        v = _validar_comuns(conn, dados_, "kits", id_)
        v.update(status=status, preco=_numero(dados_.get("preco", 0), "preco", "Preço"))
        v.update(_validar_51_kit(conn, dados_, atual, id_))
        colunas = list(v)
        if atual:
            conn.execute("UPDATE kits SET " + ", ".join(f"{c} = ?" for c in colunas)
                         + f", atualizado_em = ? WHERE id = ? AND {_t()}",
                         (*v.values(), agora_, id_))
            novo_id = id_
        else:
            r = conn.execute(
                "INSERT INTO kits (tenant_id, criado_em, atualizado_em, " + ", ".join(colunas)
                + ") VALUES (?, ?, ?, " + ", ".join("?" * len(colunas)) + ")",
                (tenant_atual(), agora_, agora_, *v.values()))
            novo_id = r.lastrowid
        _gravar_tags(conn, "tags_kit", "kit_id", novo_id, tags)
        _sincronizar_precos_kits(conn, kit_ids=[novo_id])
        v["preco"] = conn.execute("SELECT preco FROM kits WHERE id = ?", (novo_id,)).fetchone()[0]
        mudancas = _mudancas(atual, v)
        if mudancas or not atual:
            auditar(conn, "kit", novo_id, "alterar" if atual else "criar",
                    f"Kit {v['nome']} {'alterado' if atual else 'criado'}", usuario_id,
                    mudancas if atual else None,
                    tipo=f"kit_{'alterou' if atual else 'criou'}")
        return novo_id


def _composicao(conn, kit_id: int) -> dict:
    return {r["produto_id"]: r["quantidade"] for r in conn.execute(
        "SELECT produto_id, quantidade FROM itens_kit WHERE kit_id = ?", (kit_id,))}


def adicionar_item_kit(kit_id: int, produto_id: int, quantidade=1,
                       usuario_id=None, obrigatorio: bool = True) -> int:
    """Inclui (ou atualiza) um componente. Componentes são sempre produtos —
    locação, encomenda ou serviço —, então um kit nunca contém outro kit e não
    há composição circular."""
    with conectar() as conn:
        kit = conn.execute(f"SELECT * FROM kits WHERE id = ? AND {_t()}", (kit_id,)).fetchone()
        if not kit:
            raise ErroDeCampo("kit_id", "Kit não encontrado.")
        try:
            produto_id = int(produto_id)
        except (TypeError, ValueError):
            raise ErroDeCampo("produto_id", "Produto não encontrado.")
        prod = conn.execute(f"SELECT * FROM produtos WHERE id = ? AND {_t()}",
                            (produto_id,)).fetchone()
        if not prod:
            raise ErroDeCampo("produto_id", "Produto não encontrado.")
        quantidade = regras.quantidade(quantidade, prod["unidade"])
        antes = _composicao(conn, kit_id)
        existente = conn.execute("SELECT * FROM itens_kit WHERE kit_id = ? AND produto_id = ?",
                                 (kit_id, produto_id)).fetchone()
        if not existente and prod["status"] != "disponivel":
            raise ErroDeCampo("produto_id", f"'{prod['nome']}' está inativo e não pode entrar"
                                            " em um kit.")
        obrigatorio = 1 if obrigatorio else 0
        if existente:
            conn.execute("UPDATE itens_kit SET quantidade = ?, obrigatorio = ? WHERE id = ?",
                         (quantidade, obrigatorio, existente["id"]))
            item_id = existente["id"]
        else:
            item_id = conn.execute("INSERT INTO itens_kit (kit_id, produto_id, quantidade,"
                                   " obrigatorio) VALUES (?, ?, ?, ?)",
                                   (kit_id, produto_id, quantidade, obrigatorio)).lastrowid
        possiveis, restricoes = regras.modalidades_do_kit(dict(kit), _componentes(conn, kit_id))
        if not possiveis:
            raise ErroDeCampo("produto_id", "Com este componente o kit não teria nenhuma"
                                            " modalidade de atendimento. " + " ".join(restricoes))
        _sincronizar_precos_kits(conn, kit_ids=[kit_id])
        mudou = {}
        if antes.get(produto_id) != quantidade:
            mudou[f"produto {produto_id}"] = [antes.get(produto_id, 0), quantidade]
        if existente and existente["obrigatorio"] != obrigatorio:
            mudou[f"produto {produto_id} obrigatório"] = [existente["obrigatorio"], obrigatorio]
        if mudou:
            auditar(conn, "kit", kit_id, "componentes", "Componentes do kit alterados",
                    usuario_id, mudou)
        return item_id


def remover_item_kit(item_id: int, kit_id: int | None = None, usuario_id=None):
    with conectar() as conn:
        r = conn.execute(
            "SELECT ik.* FROM itens_kit ik JOIN kits k ON k.id = ik.kit_id"
            f" WHERE ik.id = ? AND {_t('k')}" + (" AND ik.kit_id = ?" if kit_id else ""),
            (item_id, kit_id) if kit_id else (item_id,)).fetchone()
        if not r:
            return
        conn.execute("DELETE FROM itens_kit WHERE id = ?", (item_id,))
        _sincronizar_precos_kits(conn, kit_ids=[r["kit_id"]])
        auditar(conn, "kit", r["kit_id"], "componentes", "Componente removido do kit",
                usuario_id, {f"produto {r['produto_id']}": [r["quantidade"], 0]})


def definir_ativo(tipo: str, id_: int, ativo: bool, usuario_id=None) -> str:
    """Ativa ou inativa (nada é excluído: pedidos antigos seguem ligados ao item)."""
    tabela, sim, nao = (("produtos", "disponivel", "inativo") if tipo == "produto"
                        else ("kits", "ativo", "inativo"))
    with conectar() as conn:
        r = conn.execute(f"SELECT nome, status FROM {tabela} WHERE id = ? AND {_t()}",
                         (id_,)).fetchone()
        if not r:
            raise ValueError("Item não encontrado.")
        novo = sim if ativo else nao
        if tipo == "kit" and ativo and not conn.execute(
                "SELECT 1 FROM itens_kit WHERE kit_id = ?", (id_,)).fetchone():
            raise ValueError("Adicione os componentes do kit antes de ativá-lo.")
        if r["status"] != novo:
            conn.execute(f"UPDATE {tabela} SET status = ?, atualizado_em = ? WHERE id = ?",
                         (novo, formato.agora(), id_))
            auditar(conn, tipo, id_, "ativar" if ativo else "inativar",
                    f"{r['nome']} {'ativado' if ativo else 'inativado'}", usuario_id,
                    {"status": [r["status"], novo]})
        return novo


def duplicar_item(tipo: str, id_: int, usuario_id=None, copiar_arquivo=None) -> int:
    """Cópia inativa e não publicada (nome, textos, preço, tags, fotos e componentes).

    copiar_arquivo(caminho_antigo, novo_id) -> caminho_novo copia cada foto para a
    pasta do novo item (os arquivos nunca são compartilhados).
    """
    item = buscar_produto(id_) if tipo == "produto" else buscar_kit(id_)
    if not item:
        raise ValueError("Item não encontrado.")
    base = {k: item.get(k) for k in ("nome", "categoria_id", "descricao", "selo", "dias_uso",
                                     "dias_antecedencia", "seo_titulo", "seo_descricao")}
    base.update(nome=f"{item['nome']} (cópia)"[:120], publicado=0, destaque=0, slug="")
    if tipo == "produto":
        base.update({k: item.get(k) for k in ("preco_locacao", "valor_referencia",
                                              "quantidade_total", "reuso_mesmo_dia",
                                              "localizacao", "observacoes",
                                              *_CAMPOS_51_PRODUTO)}, status="inativo")
        base["tipo"] = base.get("tipo") or None
        novo = salvar_produto(base, tags=item.get("tags") or [], usuario_id=usuario_id)
    else:
        base.update(preco=item.get("preco"), status="inativo", codigo_sku="",
                    **{k: item.get(k) for k in ("modo_preco", "permite_retirada",
                                                "permite_montagem")})
        novo = salvar_kit(base, tags=item.get("tags") or [], usuario_id=usuario_id)
        for c in item["itens"]:
            with conectar() as conn:  # a cópia mantém peças já inativas
                conn.execute("INSERT INTO itens_kit (kit_id, produto_id, quantidade, obrigatorio)"
                             " VALUES (?, ?, ?, ?)", (novo, c["produto_id"], c["quantidade"],
                                                      c.get("obrigatorio", 1)))
        with conectar() as conn:
            _sincronizar_precos_kits(conn, kit_ids=[novo])
    for f in item.get("fotos") or []:
        if copiar_arquivo:
            arquivo = copiar_arquivo(f["arquivo"], novo)
            mini = copiar_arquivo(f["miniatura"], novo) if f.get("miniatura") else ""
            if arquivo:
                (salvar_foto_produto if tipo == "produto" else salvar_foto_kit)(
                    novo, arquivo, bool(f["principal"]), mini or "")
    return novo


def mover_foto(tipo: str, item_id: int, foto_id: int, passo: int):
    """Troca a posição da foto com a vizinha (ordem da galeria)."""
    tabela, coluna, dono = (("fotos_produto", "produto_id", "produtos") if tipo == "produto"
                            else ("fotos_kit", "kit_id", "kits"))
    with conectar() as conn:
        if not _do_tenant(conn, dono, item_id):
            return
        fotos = [dict(r) for r in conn.execute(
            f"SELECT id FROM {tabela} WHERE {coluna} = ? ORDER BY principal DESC, ordem, id",
            (item_id,))]
        ids = [f["id"] for f in fotos]
        if foto_id not in ids:
            return
        i = ids.index(foto_id)
        j = i + (1 if passo > 0 else -1)
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        for ordem, fid in enumerate(ids, start=1):
            conn.execute(f"UPDATE {tabela} SET ordem = ? WHERE id = ?", (ordem, fid))
        # a primeira da galeria é a capa
        conn.execute(f"UPDATE {tabela} SET principal = CASE WHEN id = ? THEN 1 ELSE 0 END"
                     f" WHERE {coluna} = ?", (ids[0], item_id))


# --- Lista administrativa (produtos e kits juntos) -------------------------------

ABAS_CATALOGO = (("todos", "Todos"), ("kits", "Kits"), ("pecas", "Peças avulsas"),
                 ("ativos", "Ativos"), ("inativos", "Inativos"),
                 ("classificar", "A classificar"))
# Natureza para filtro e selo: kit ou o tipo do produto ("classificar" = vazio)
NATUREZAS = (("kit", "Kit"), ("locacao", "Locação"), ("encomenda", "Sob encomenda"),
             ("servico", "Serviço"), ("classificar", "A classificar"))


def _capa_sql(tabela_fotos: str, coluna: str, alias: str) -> str:
    return (f"(SELECT CASE WHEN miniatura != '' THEN miniatura ELSE arquivo END"
            f" FROM {tabela_fotos} WHERE {coluna} = {alias}.id"
            " ORDER BY principal DESC, ordem, id LIMIT 1)")


def itens_catalogo_admin() -> list:
    """Produtos e kits da empresa numa lista só (3 consultas, sem N+1)."""
    with conectar() as conn:
        produtos = [dict(r, tipo="produto") for r in conn.execute(
            "SELECT p.id, p.nome, p.codigo_sku, p.categoria_id, c.nome AS categoria_nome,"
            " p.descricao, p.preco_locacao AS preco, p.quantidade_total, p.status, p.publicado,"
            " p.destaque, p.selo, p.slug, p.localizacao, p.tipo AS natureza, p.unidade,"
            f" {_capa_sql('fotos_produto', 'produto_id', 'p')} AS capa,"
            " (SELECT GROUP_CONCAT(tag, '|') FROM tags_produto t WHERE t.produto_id = p.id) AS tags,"
            " (SELECT COUNT(DISTINCT kit_id) FROM itens_kit ik WHERE ik.produto_id = p.id) AS em_kits,"
            " (SELECT COUNT(DISTINCT pedido_id) FROM itens_pedido ip WHERE ip.tipo = 'produto'"
            "   AND ip.item_id = p.id) AS em_pedidos"
            " FROM produtos p LEFT JOIN categorias c ON c.id = p.categoria_id"
            f" WHERE {_t('p')}")]
        kits = [dict(r, tipo="kit") for r in conn.execute(
            "SELECT k.id, k.nome, COALESCE(k.codigo_sku, '') AS codigo_sku, k.categoria_id,"
            " c.nome AS categoria_nome, k.modo_preco, 'kit' AS natureza, 'pacote' AS unidade,"
            " k.descricao, k.preco, k.status, k.publicado, k.destaque, k.selo, k.slug, 0 AS em_kits,"
            " (SELECT COUNT(DISTINCT pedido_id) FROM itens_pedido ip WHERE ip.tipo = 'kit'"
            "   AND ip.item_id = k.id) AS em_pedidos,"
            f" {_capa_sql('fotos_kit', 'kit_id', 'k')} AS capa,"
            " (SELECT GROUP_CONCAT(tag, '|') FROM tags_kit t WHERE t.kit_id = k.id) AS tags,"
            " (SELECT COUNT(*) FROM itens_kit ik WHERE ik.kit_id = k.id) AS componentes,"
            " (SELECT SUM(ik.quantidade * p.preco_locacao) FROM itens_kit ik"
            "   JOIN produtos p ON p.id = ik.produto_id WHERE ik.kit_id = k.id AND ik.obrigatorio = 1) AS soma_produtos"
            " FROM kits k LEFT JOIN categorias c ON c.id = k.categoria_id"
            f" WHERE {_t('k')}")]
    for i in produtos + kits:
        i["tags"] = [t for t in (i.get("tags") or "").split("|") if t]
        i["ativo"] = i["status"] in ("disponivel", "ativo")
        i["natureza"] = i.get("natureza") or "classificar"
        if i["natureza"] == "classificar":
            i["sugestao"] = regras.sugerir_tipo(i["nome"], i.get("descricao"))[0]
        if i["tipo"] == "kit":
            soma = i.get("soma_produtos") or 0
            i["economia"] = round((1 - i["preco"] / soma) * 100) if soma and i["preco"] < soma else 0
    return sorted(produtos + kits, key=lambda i: normalizar_texto(i["nome"]))


def filtrar_catalogo_admin(itens: list, aba: str = "todos", busca: str = "",
                           categoria_id=None, tipo: str = "", status: str = "",
                           preco_min=None, preco_max=None, estoque: str = "",
                           tag: str = "", natureza: str = "") -> list:
    def na_aba(i):
        return {"todos": True, "kits": i["tipo"] == "kit" and i["ativo"],
                "pecas": i["tipo"] == "produto" and i["ativo"],
                "ativos": i["ativo"], "inativos": i["status"] == "inativo",
                "classificar": i.get("natureza") == "classificar"}.get(aba, True)
    termo = normalizar_texto(busca).strip()
    saida = []
    for i in itens:
        if not na_aba(i):
            continue
        if tipo and i["tipo"] != tipo:
            continue
        if natureza and i.get("natureza") != natureza:
            continue
        if status and i["status"] != status:
            continue
        if categoria_id and i["categoria_id"] != categoria_id:
            continue
        if tag and tag not in i["tags"]:
            continue
        if preco_min is not None and (i["preco"] or 0) < preco_min:
            continue
        if preco_max is not None and (i["preco"] or 0) > preco_max:
            continue
        if estoque == "com" and not _tem_estoque(i):
            continue
        if estoque == "sem" and _tem_estoque(i):
            continue
        if termo and termo not in normalizar_texto(" ".join(
                [i["nome"], i["codigo_sku"] or "", i["descricao"] or "",
                 i["categoria_nome"] or "", " ".join(i["tags"]), i.get("localizacao") or ""])):
            continue
        saida.append(i)
    return saida


def _tem_estoque(i: dict) -> bool:
    if i["tipo"] == "produto":
        if not regras.consome_estoque(None if i.get("natureza") == "classificar"
                                      else i.get("natureza")):
            return i["status"] == "disponivel"
        return i["status"] == "disponivel" and (i["quantidade_total"] or 0) > 0
    d = disponibilidade_kit(i["id"])
    return i["status"] == "ativo" if d is None else d > 0


def classificar_produtos(ids: list, tipo: str, usuario_id=None) -> int:
    """Confirma o tipo de produtos ainda a classificar (em lote, com auditoria).
    Só mexe em quem está sem tipo; a unidade vai para a padrão do tipo."""
    if tipo not in regras.TIPOS_ITEM:
        raise ErroDeCampo("tipo", "Escolha o tipo.")
    feitos = 0
    with conectar() as conn:
        for pid in ids:
            r = conn.execute(f"SELECT id, nome, tipo, unidade FROM produtos WHERE id = ? AND {_t()}",
                             (pid,)).fetchone()
            if not r or r["tipo"]:
                continue
            unidade = r["unidade"] if r["unidade"] in regras.UNIDADES_POR_TIPO[tipo] \
                else regras.UNIDADE_PADRAO[tipo]
            extra = ", permite_retirada = 0, local_execucao = 'evento'" if tipo == "servico" else ""
            conn.execute(f"UPDATE produtos SET tipo = ?, unidade = ?, atualizado_em = ?{extra}"
                         " WHERE id = ?", (tipo, unidade, formato.agora(), pid))
            auditar(conn, "produto", pid, "classificar", f"{r['nome']}: tipo confirmado",
                    usuario_id, {"tipo": [None, tipo], "unidade": [r["unidade"], unidade]})
            feitos += 1
    return feitos


def contagem_abas_catalogo(itens: list) -> dict:
    return {chave: len(filtrar_catalogo_admin(itens, chave)) for chave, _ in ABAS_CATALOGO}


# --- Categorias (imagem, endereço e visibilidade) --------------------------------

def salvar_categoria_completa(nome: str, pai_id=None, id_=None, descricao: str = "",
                              visivel: bool = True, slug: str = "", usuario_id=None) -> int:
    cid = salvar_categoria(nome, pai_id, id_)
    with conectar() as conn:
        antes = dict(conn.execute("SELECT * FROM categorias WHERE id = ?", (cid,)).fetchone())
        slug = (slug or "").strip().lower()
        if slug and (not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug) or slug in SLUGS_RESERVADOS):
            raise ErroDeCampo("slug", "Endereço inválido: use letras minúsculas, números e hífens.")
        final = _slug_livre(conn, tenant_atual(), nome, "categorias", cid, slug)
        if slug and final != slug:
            raise ErroDeCampo("slug", "Já existe um item ou categoria com este endereço.")
        conn.execute("UPDATE categorias SET descricao = ?, visivel = ?, slug = ? WHERE id = ?",
                     ((descricao or "").strip()[:300], 1 if visivel else 0, final, cid))
        depois = {"nome": nome.strip(), "descricao": (descricao or "").strip()[:300],
                  "visivel": 1 if visivel else 0, "slug": final}
        mudancas = {k: [antes.get(k), v] for k, v in depois.items() if str(antes.get(k)) != str(v)}
        if mudancas:
            auditar(conn, "categoria", cid, "alterar" if id_ else "criar",
                    f"Categoria {nome.strip()} {'alterada' if id_ else 'criada'}", usuario_id,
                    mudancas)
    return cid


def salvar_imagem_categoria(id_: int, arquivo: str, usuario_id=None):
    with conectar() as conn:
        r = conn.execute(f"SELECT imagem FROM categorias WHERE id = ? AND {_t()}", (id_,)).fetchone()
        if not r:
            raise ValueError("Categoria não encontrada.")
        conn.execute("UPDATE categorias SET imagem = ? WHERE id = ?", (arquivo, id_))
        auditar(conn, "categoria", id_, "imagem", "Imagem da categoria alterada", usuario_id,
                {"imagem": [r["imagem"], arquivo]})


def pedidos_do_item(tipo: str, id_: int, inicio: str, fim: str) -> list:
    """Pedidos em andamento que ocupam o item (ou os componentes do kit) no período.

    A Agenda continua sendo a fonte das datas: aqui só aparece quem ocupa.
    """
    with conectar() as conn:
        if tipo == "produto":
            ids = [id_]
        else:
            ids = [c["produto_id"] for c in _componentes(conn, id_)]
        ocup = _ocupacoes(conn, ids, inicio, fim)
        pedidos = sorted({o["pedido_id"] for o in ocup})
        if not pedidos:
            return []
        marcas = ",".join("?" * len(pedidos))
        linhas = [dict(r) for r in conn.execute(
            "SELECT p.id, p.data_evento, p.data_retirada, p.data_devolucao,"
            " p.status_operacional, c.nome AS cliente_nome FROM pedidos p"
            " LEFT JOIN clientes c ON c.id = p.cliente_id"
            f" WHERE p.id IN ({marcas}) AND {_t('p')}"
            " ORDER BY COALESCE(p.data_retirada, p.data_evento), p.id", pedidos)]
    por_pedido: dict = {}
    for o in ocup:
        por_pedido.setdefault(o["pedido_id"], {}).setdefault(o["produto_id"], 0)
        por_pedido[o["pedido_id"]][o["produto_id"]] += o["qtd"]
    for p in linhas:
        p["unidades"] = sum(por_pedido.get(p["id"], {}).values())
        p["rotulo_status"] = ROTULOS_OPERACIONAL.get(p["status_operacional"], p["status_operacional"])
    return linhas


def contagem_por_categoria() -> dict:
    """{categoria_id: produtos + kits ativos} (subcategorias somam na principal)."""
    with conectar() as conn:
        contagem: dict = {}
        for tabela, ativo in (("produtos", "disponivel"), ("kits", "ativo")):
            for r in conn.execute(f"SELECT categoria_id, COUNT(*) AS n FROM {tabela}"
                                  f" WHERE {_t()} AND status = ? AND categoria_id IS NOT NULL"
                                  " GROUP BY categoria_id", (ativo,)):
                contagem[r["categoria_id"]] = contagem.get(r["categoria_id"], 0) + r["n"]
        for c in conn.execute(f"SELECT id, pai_id FROM categorias WHERE {_t()} AND pai_id IS NOT NULL"):
            contagem[c["pai_id"]] = contagem.get(c["pai_id"], 0) + contagem.get(c["id"], 0)
    return contagem


# ---------------------------------------------------------------------------
# Sprint 5.1 — Materiais e receitas de produção (produtos sob encomenda)
#
# O consumo previsto é sempre calculado (receita × quantidade vendida) e nunca
# gravado. Consultar, orçar ou montar a lista da vitrine não movimenta nada.
# A tabela de movimentos fica pronta para o Sprint 6 (reserva na confirmação,
# baixa na produção); por enquanto só aceita lançamentos manuais auditados.
# ---------------------------------------------------------------------------

TIPOS_MOVIMENTO = {"entrada": 1, "estorno": 1, "baixa": -1, "perda": -1, "ajuste": 1,
                   "reserva": 0}
MOVIMENTOS_MANUAIS = {"entrada": "Entrada (compra)", "perda": "Perda ou quebra",
                      "ajuste": "Ajuste de inventário (+/-)"}


def _saldo_sql(alias="m") -> str:
    return ("(SELECT COALESCE(SUM(CASE mv.tipo WHEN 'entrada' THEN mv.quantidade"
            " WHEN 'estorno' THEN mv.quantidade WHEN 'ajuste' THEN mv.quantidade"
            " WHEN 'baixa' THEN -mv.quantidade WHEN 'perda' THEN -mv.quantidade ELSE 0 END), 0)"
            f" FROM movimentos_material mv WHERE mv.material_id = {alias}.id)")


def listar_materiais(somente_ativos: bool = False, busca: str = "") -> list:
    with conectar() as conn:
        linhas = [dict(r) for r in conn.execute(
            f"SELECT m.*, {_saldo_sql()} AS saldo,"
            " (SELECT COUNT(*) FROM receitas_produto rp WHERE rp.material_id = m.id) AS em_receitas"
            f" FROM materiais m WHERE {_t('m')}" + (" AND m.ativo = 1" if somente_ativos else "")
            + " ORDER BY m.ativo DESC, m.nome")]
    termo = normalizar_texto(busca).strip()
    if termo:
        linhas = [m for m in linhas if termo in normalizar_texto(f"{m['nome']} {m['codigo']}")]
    for m in linhas:
        m["saldo"] = round(m["saldo"] or 0, regras.CASAS)
    return linhas


def buscar_material(id_: int) -> dict | None:
    with conectar() as conn:
        r = conn.execute(f"SELECT m.*, {_saldo_sql()} AS saldo FROM materiais m"
                         f" WHERE m.id = ? AND {_t('m')}", (id_,)).fetchone()
        if not r:
            return None
        m = dict(r)
        m["movimentos"] = [dict(x) for x in conn.execute(
            "SELECT mv.*, u.nome AS usuario_nome FROM movimentos_material mv"
            " LEFT JOIN usuarios u ON u.id = mv.usuario_id WHERE mv.material_id = ?"
            " ORDER BY mv.id DESC LIMIT 30", (id_,))]
        m["produtos"] = [dict(x) for x in conn.execute(
            "SELECT p.id, p.nome, p.unidade, rp.quantidade FROM receitas_produto rp"
            f" JOIN produtos p ON p.id = rp.produto_id WHERE rp.material_id = ? AND {_t('p')}"
            " ORDER BY p.nome", (id_,))]
        return m


def salvar_material(d: dict, id_: int | None = None, usuario_id=None) -> int:
    nome = " ".join(str(d.get("nome") or "").split())[:120]
    if not nome:
        raise ErroDeCampo("nome", "Nome do material é obrigatório.")
    codigo = " ".join(str(d.get("codigo") or "").split()).upper()[:40]
    unidade = d.get("unidade") or "un"
    if unidade not in regras.UNIDADES_MATERIAL:
        raise ErroDeCampo("unidade", "Unidade de medida inválida.")
    arred = d.get("arredondamento") or "exato"
    if arred not in regras.ARREDONDAMENTOS:
        raise ErroDeCampo("arredondamento", "Regra de arredondamento inválida.")
    custo = d.get("custo_referencia")
    custo = None if custo in (None, "") else _numero(custo, "custo_referencia", "Custo")
    v = {"nome": nome, "codigo": codigo, "unidade": unidade, "arredondamento": arred,
         "custo_referencia": custo, "ativo": 1 if d.get("ativo", 1) else 0,
         "observacoes": str(d.get("observacoes") or "").strip()[:500]}
    agora_ = formato.agora()
    with conectar() as conn:
        atual = None
        if id_:
            r = conn.execute(f"SELECT * FROM materiais WHERE id = ? AND {_t()}", (id_,)).fetchone()
            if not r:
                raise ValueError("Material não encontrado.")
            atual = dict(r)
        if conn.execute(f"SELECT 1 FROM materiais WHERE nome = ? AND id != ? AND {_t()}",
                        (nome, id_ or 0)).fetchone():
            raise ErroDeCampo("nome", "Já existe um material com este nome.")
        if codigo and conn.execute(f"SELECT 1 FROM materiais WHERE codigo = ? AND id != ? AND {_t()}",
                                   (codigo, id_ or 0)).fetchone():
            raise ErroDeCampo("codigo", "Já existe um material com este código.")
        if atual:
            conn.execute("UPDATE materiais SET " + ", ".join(f"{c} = ?" for c in v)
                         + f", atualizado_em = ? WHERE id = ? AND {_t()}", (*v.values(), agora_, id_))
            novo = id_
        else:
            novo = conn.execute(
                "INSERT INTO materiais (tenant_id, criado_em, atualizado_em, " + ", ".join(v)
                + ") VALUES (?, ?, ?, " + ", ".join("?" * len(v)) + ")",
                (tenant_atual(), agora_, agora_, *v.values())).lastrowid
        mud = {c: [(atual or {}).get(c), v[c]] for c in v
               if str((atual or {}).get(c) if (atual or {}).get(c) is not None else "")
               != str(v[c] if v[c] is not None else "")}
        if mud:
            auditar(conn, "material", novo, "alterar" if atual else "criar",
                    f"Material {nome} {'alterado' if atual else 'criado'}", usuario_id,
                    mud if atual else None)
        return novo


def registrar_movimento_material(material_id: int, tipo: str, quantidade, observacao: str = "",
                                 usuario_id=None, pedido_id=None, manual: bool = True) -> int:
    """Lançamento rastreável. Manual: entrada, perda e ajuste (com sinal).
    Reserva, baixa e estorno ficam para o fluxo de pedidos do Sprint 6."""
    if tipo not in TIPOS_MOVIMENTO or (manual and tipo not in MOVIMENTOS_MANUAIS):
        raise ErroDeCampo("tipo", "Tipo de movimentação inválido.")
    qtd = round(regras.numero(quantidade, "quantidade"), regras.CASAS)
    if qtd == 0 or (tipo != "ajuste" and qtd < 0):
        raise ErroDeCampo("quantidade", "Informe uma quantidade maior que zero"
                          + (" (no ajuste, use negativo para diminuir)." if tipo == "ajuste" else "."))
    observacao = str(observacao or "").strip()[:300]
    if tipo in ("perda", "ajuste") and not observacao:
        raise ErroDeCampo("observacao", "Explique o motivo da perda ou do ajuste.")
    with conectar() as conn:
        m = conn.execute(f"SELECT * FROM materiais WHERE id = ? AND {_t()}", (material_id,)).fetchone()
        if not m:
            raise ValueError("Material não encontrado.")
        if m["arredondamento"] == "inteiro_acima" and qtd != int(qtd):
            raise ErroDeCampo("quantidade", f"{m['nome']} é contado em unidades inteiras.")
        if pedido_id and not _do_tenant(conn, "pedidos", pedido_id):
            raise ErroDeCampo("pedido_id", "Pedido não encontrado.")
        mov = conn.execute(
            "INSERT INTO movimentos_material (tenant_id, material_id, tipo, quantidade, pedido_id,"
            " usuario_id, observacao, criado_em) VALUES (?,?,?,?,?,?,?,?)",
            (tenant_atual(), material_id, tipo, qtd, pedido_id, usuario_id, observacao,
             formato.agora())).lastrowid
        auditar(conn, "material", material_id, f"movimento_{tipo}",
                f"{m['nome']}: {tipo} de {regras.formatar_qtd(qtd)} {m['unidade']}", usuario_id,
                tipo="material_movimento",
                dados={"movimento_id": mov, "tipo": tipo, "quantidade": qtd, "pedido_id": pedido_id})
        return mov


def receita_do_produto(produto_id: int) -> list:
    with conectar() as conn:
        return _receita(conn, produto_id)


def _receita(conn, produto_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT rp.id, rp.material_id, rp.quantidade, rp.observacao, m.nome, m.codigo,"
        " m.unidade, m.arredondamento, m.custo_referencia, m.ativo"
        f" FROM receitas_produto rp JOIN materiais m ON m.id = rp.material_id AND {_t('m')}"
        " WHERE rp.produto_id = ? ORDER BY m.nome", (produto_id,))]


def salvar_linha_receita(produto_id: int, material_id, quantidade, observacao: str = "",
                         usuario_id=None) -> int:
    """Material consumido por 1 unidade de cobrança do produto (ex.: por metro)."""
    with conectar() as conn:
        p = conn.execute(f"SELECT * FROM produtos WHERE id = ? AND {_t()}", (produto_id,)).fetchone()
        if not p:
            raise ValueError("Produto não encontrado.")
        if p["tipo"] != "encomenda":
            raise ErroDeCampo("material_id", "Receita de materiais é só para produtos sob encomenda.")
        try:
            material_id = int(material_id)
        except (TypeError, ValueError):
            raise ErroDeCampo("material_id", "Escolha o material.")
        m = conn.execute(f"SELECT * FROM materiais WHERE id = ? AND {_t()}", (material_id,)).fetchone()
        if not m:
            raise ErroDeCampo("material_id", "Material não encontrado.")
        qtd = round(regras.numero(quantidade, "quantidade"), 6)
        if qtd <= 0:
            raise ErroDeCampo("quantidade", "A quantidade consumida precisa ser maior que zero.")
        existente = conn.execute("SELECT * FROM receitas_produto WHERE produto_id = ? AND material_id = ?",
                                 (produto_id, material_id)).fetchone()
        if not existente and not m["ativo"]:
            raise ErroDeCampo("material_id", f"'{m['nome']}' está inativo.")
        observacao = str(observacao or "").strip()[:200]
        if existente:
            conn.execute("UPDATE receitas_produto SET quantidade = ?, observacao = ? WHERE id = ?",
                         (qtd, observacao, existente["id"]))
            linha = existente["id"]
        else:
            linha = conn.execute("INSERT INTO receitas_produto (produto_id, material_id, quantidade,"
                                 " observacao) VALUES (?,?,?,?)",
                                 (produto_id, material_id, qtd, observacao)).lastrowid
        antes = existente["quantidade"] if existente else 0
        if antes != qtd:
            auditar(conn, "produto", produto_id, "receita", f"Receita de {p['nome']} alterada",
                    usuario_id, {f"material {m['nome']}": [antes, qtd]})
        return linha


def remover_linha_receita(linha_id: int, produto_id: int, usuario_id=None):
    with conectar() as conn:
        r = conn.execute(
            "SELECT rp.*, m.nome FROM receitas_produto rp JOIN produtos p ON p.id = rp.produto_id"
            f" JOIN materiais m ON m.id = rp.material_id WHERE rp.id = ? AND rp.produto_id = ? AND {_t('p')}",
            (linha_id, produto_id)).fetchone()
        if not r:
            return
        conn.execute("DELETE FROM receitas_produto WHERE id = ?", (linha_id,))
        auditar(conn, "produto", produto_id, "receita", "Material retirado da receita", usuario_id,
                {f"material {r['nome']}": [r["quantidade"], 0]})


def consumo_previsto_produto(produto_id: int, quantidade) -> dict:
    """Materiais previstos para produzir 'quantidade' (na unidade de cobrança).
    Só calcula: não reserva nem baixa nada."""
    with conectar() as conn:
        p = conn.execute(f"SELECT * FROM produtos WHERE id = ? AND {_t()}", (produto_id,)).fetchone()
        if not p:
            raise ValueError("Produto não encontrado.")
        qtd = regras.quantidade(quantidade, p["unidade"])
        linhas = regras.consumo_previsto(_receita(conn, produto_id), qtd)
    custos = [ln["custo"] for ln in linhas if ln["custo"] is not None]
    return {"produto_id": produto_id, "nome": p["nome"], "quantidade": qtd, "unidade": p["unidade"],
            "materiais": linhas, "custo_total": round(sum(custos), 2) if custos else None,
            "custo_incompleto": len(custos) < len(linhas)}


def consumo_previsto_pedido(pedido_id: int) -> list:
    """Materiais previstos para o pedido (encomendas avulsas e dentro de kits),
    somados por material. Base para a reserva do Sprint 6."""
    with conectar() as conn:
        if not _do_tenant(conn, "pedidos", pedido_id):
            return []
        total: dict = {}
        for ip in conn.execute("SELECT * FROM itens_pedido WHERE pedido_id = ?", (pedido_id,)).fetchall():
            pares = []
            if ip["tipo"] == "produto" and ip["item_id"]:
                pares.append((ip["item_id"], ip["quantidade"]))
            elif ip["tipo"] == "kit" and ip["item_id"]:
                comps = _ler_composicao(ip["composicao"]) or _composicao_venda(conn, ip["item_id"])
                pares += [(c["produto_id"], ip["quantidade"] * float(c["quantidade"]))
                          for c in comps if c.get("obrigatorio", 1) and c.get("tipo") == "encomenda"]
            for pid, qtd in pares:
                for ln in regras.consumo_previsto(_receita(conn, pid), qtd):
                    t = total.setdefault(ln["material_id"], dict(ln, previsto=0, calculado=0))
                    t["previsto"] += ln["previsto"]
                    t["calculado"] = round(t["calculado"] + ln["calculado"], regras.CASAS)
        return sorted(total.values(), key=lambda m: normalizar_texto(m["nome"]))


def resumo_kit(kit_id: int) -> dict | None:
    """Composição e preço oficiais do kit (usados pela tela e pela API)."""
    with conectar() as conn:
        k = conn.execute(f"SELECT * FROM kits WHERE id = ? AND {_t()}", (kit_id,)).fetchone()
        if not k:
            return None
        comps = _componentes(conn, kit_id)
    ref = regras.preco_referencia(comps)
    final = regras.preco_final_kit(k["modo_preco"], k["preco"], comps)
    possiveis, restricoes = regras.modalidades_do_kit(dict(k), comps)
    for c in comps:
        c["subtotal"] = round(float(c["preco"] or 0) * float(c["quantidade"]), 2)
        c["rotulo_tipo"] = regras.rotulo_tipo(c["tipo"])
        c["sigla"] = regras.sigla(c["unidade"])
    return {"kit_id": kit_id, "modo_preco": k["modo_preco"], "componentes": comps,
            "referencia": ref, "preco_final": final,
            "diferenca": regras.diferenca(ref["obrigatorios"], final),
            "modalidades": sorted(possiveis), "restricoes": restricoes,
            "inativos": [c["nome"] for c in comps if c["status"] != "disponivel"]}


def itens_para_venda() -> list:
    """Produtos e kits ativos para escolher no orçamento e no pedido
    (preço atual sugerido; a linha grava o preço do momento)."""
    with conectar() as conn:
        prods = [dict(r, tipo="produto") for r in conn.execute(
            "SELECT id, nome, codigo_sku, preco_locacao AS preco, unidade, tipo AS natureza"
            f" FROM produtos WHERE status = 'disponivel' AND {_t()} ORDER BY nome")]
        kits = [dict(r, tipo="kit", unidade="pacote", natureza="kit") for r in conn.execute(
            "SELECT k.id, k.nome, k.codigo_sku, k.preco FROM kits k"
            f" WHERE k.status = 'ativo' AND {_t('k')}"
            " AND EXISTS (SELECT 1 FROM itens_kit ik WHERE ik.kit_id = k.id) ORDER BY k.nome")]
    for i in prods + kits:
        i["sigla"] = regras.sigla(i["unidade"])
        i["fracao"] = regras.aceita_fracao(i["unidade"])
        i["rotulo_tipo"] = "Kit" if i["tipo"] == "kit" else regras.rotulo_tipo(i["natureza"])
    return prods + kits
