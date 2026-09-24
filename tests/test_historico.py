"""Testes da importacao de eventos historicos do Morumbi 3D."""

import os
import sqlite3
import tempfile

import pytest

from sistema import dados


def _criar_banco_3d(caminho: str):
    """Cria um banco SQLite do 3D minimo para testes."""
    conn = sqlite3.connect(caminho)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE clientes (
            id INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            whatsapp TEXT DEFAULT '',
            email TEXT DEFAULT '',
            cpf TEXT DEFAULT '',
            endereco TEXT DEFAULT '',
            complemento TEXT DEFAULT '',
            bairro TEXT DEFAULT '',
            cidade TEXT DEFAULT '',
            cep TEXT DEFAULT '',
            canal TEXT DEFAULT '',
            observacao TEXT DEFAULT '',
            ativo INTEGER DEFAULT 1,
            criado_em TEXT DEFAULT '',
            criado_por TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE pedidos (
            id INTEGER PRIMARY KEY,
            cliente TEXT NOT NULL,
            cliente_id INTEGER REFERENCES clientes(id),
            canal TEXT DEFAULT '',
            prazo TEXT,
            valor REAL,
            desconto REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'orcamento',
            observacao TEXT DEFAULT '',
            criado_em TEXT NOT NULL,
            entregue_em TEXT
        )
    """)
    conn.commit()
    return conn


def _popular_3d(conn_3d):
    """Insere dados de teste no banco 3D."""
    conn_3d.execute(
        "INSERT INTO clientes (id, nome, whatsapp, criado_em)"
        " VALUES (1, 'Maria Silva', '67999001234', '2024-01-01')")
    conn_3d.execute(
        "INSERT INTO clientes (id, nome, whatsapp, criado_em)"
        " VALUES (2, 'Joao Santos', '67999005678', '2024-02-01')")
    conn_3d.execute(
        "INSERT INTO clientes (id, nome, criado_em)"
        " VALUES (3, 'Sem Vinculo', '2024-03-01')")

    conn_3d.executemany(
        "INSERT INTO pedidos (id, cliente, cliente_id, canal, prazo, valor,"
        " status, observacao, criado_em, entregue_em)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "Maria Silva", 1, "WhatsApp", "2024-06-15", 500.0,
             "entregue", "Festa junina", "2024-05-01", "2024-06-15"),
            (2, "Maria Silva", 1, "Instagram", "2024-09-20", 800.0,
             "entregue", "Aniversario", "2024-08-01", "2024-09-20"),
            (3, "Joao Santos", 2, "Balcao", "2024-07-10", 300.0,
             "entregue", "", "2024-06-01", "2024-07-10"),
            (4, "Joao Santos", 2, "WhatsApp", "2024-11-01", 600.0,
             "aprovado", "Pendente", "2024-10-01", None),
            (5, "Maria Silva", 1, "Loja", "2024-12-25", 1200.0,
             "cancelado", "Cancelou", "2024-11-01", None),
            (6, "Sem Vinculo", 3, "Balcao", "2024-08-01", 200.0,
             "entregue", "", "2024-07-01", "2024-08-01"),
            (7, "Maria Silva", 1, "WhatsApp", "2025-01-15", 0,
             "orcamento", "So orcamento", "2025-01-01", None),
        ])
    conn_3d.commit()


def _vincular_clientes(cliente_3d_1=1, cliente_3d_2=2):
    """Cria clientes no Festas vinculados ao 3D."""
    with dados.conectar() as conn:
        conn.execute(
            "INSERT INTO clientes (nome, whatsapp, status, cliente_3d_id,"
            " criado_em, atualizado_em)"
            " VALUES ('Maria Silva', '67999001234', 'ativo', ?,"
            " '2024-01-01', '2024-01-01')",
            (cliente_3d_1,))
        conn.execute(
            "INSERT INTO clientes (nome, whatsapp, status, cliente_3d_id,"
            " criado_em, atualizado_em)"
            " VALUES ('Joao Santos', '67999005678', 'ativo', ?,"
            " '2024-02-01', '2024-02-01')",
            (cliente_3d_2,))


class TesteMigracaoEventosHistorico:
    def test_tabela_criada(self, app):
        with dados.conectar() as conn:
            r = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='eventos_historico'").fetchone()
            assert r is not None

    def test_indice_unico_origem(self, app):
        with dados.conectar() as conn:
            r = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index'"
                " AND name='ux_evt_hist_tenant_origem'").fetchone()
            # Sprint 2.2: a chave de importação é única dentro de cada empresa
            assert r is not None and "tenant_id, origem, origem_id" in r[0]

    def test_colunas_classificacao_no_cliente(self, app):
        with dados.conectar() as conn:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(clientes)").fetchall()]
            assert "total_festas" in cols
            assert "classificacao" in cols
            assert "ultima_festa" in cols

    def test_classificacao_default_sem_historico(self, app):
        with dados.conectar() as conn:
            conn.execute(
                "INSERT INTO clientes (nome, status, criado_em, atualizado_em)"
                " VALUES ('Teste', 'ativo', '2024-01-01', '2024-01-01')")
            r = conn.execute(
                "SELECT classificacao, total_festas FROM clientes"
                " WHERE nome='Teste'").fetchone()
            assert r["classificacao"] == "Sem histórico"
            assert r["total_festas"] == 0


class TesteClassificacaoFestas:
    def test_zero_sem_historico(self):
        assert dados.classificar_festas(0) == "Sem histórico"

    def test_uma_festa(self):
        assert dados.classificar_festas(1) == "Cliente de 1 festa"

    def test_recorrente(self):
        assert dados.classificar_festas(2) == "Cliente recorrente"
        assert dados.classificar_festas(4) == "Cliente recorrente"

    def test_frequente(self):
        assert dados.classificar_festas(5) == "Cliente frequente"
        assert dados.classificar_festas(9) == "Cliente frequente"

    def test_vip(self):
        assert dados.classificar_festas(10) == "Cliente VIP histórico"
        assert dados.classificar_festas(50) == "Cliente VIP histórico"


class TesteSalvarEventoHistorico:
    def test_salvar_evento(self, app):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        eid = dados.salvar_evento_historico({
            "cliente_id": cid,
            "origem_id": 1,
            "origem": "Morumbi 3D",
            "data_evento": "2024-06-15",
            "descricao": "Festa junina",
            "canal": "WhatsApp",
            "valor": 500.0,
            "status_origem": "entregue",
        })
        assert eid > 0

    def test_idempotencia_origem_id(self, app):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 100,
            "data_evento": "2024-06-15", "status_origem": "entregue",
        })
        with pytest.raises(Exception):
            dados.salvar_evento_historico({
                "cliente_id": cid, "origem_id": 100,
                "data_evento": "2024-06-16", "status_origem": "entregue",
            })


class TesteEventosHistorico:
    def test_listar_por_periodo(self, app):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 1, "origem": "Formulario Festas",
            "data_evento": "2024-06-15", "descricao": "Festa 1",
            "status_origem": "entregue",
        })
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 2, "origem": "Formulario Festas",
            "data_evento": "2024-09-20", "descricao": "Festa 2",
            "status_origem": "entregue",
        })
        evts = dados.eventos_historico("2024-06-01", "2024-06-30")
        assert len(evts) == 1
        assert evts[0]["descricao"] == "Festa 1"

    def test_inclui_nome_cliente(self, app):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 10, "origem": "Formulario Festas",
            "data_evento": "2024-06-15", "status_origem": "entregue",
        })
        evts = dados.eventos_historico("2024-06-01", "2024-06-30")
        assert evts[0]["cliente_nome"] == "Maria Silva"


class TesteAtualizarClassificacao:
    def test_atualiza_total_e_classificacao(self, app):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        for i in range(3):
            dados.salvar_evento_historico({
                "cliente_id": cid, "origem_id": 200 + i, "origem": "Formulario Festas",
                "data_evento": f"2024-0{i+1}-15",
                "status_origem": "entregue",
            })
        dados.atualizar_classificacao_cliente(cid)
        with dados.conectar() as conn:
            c = dict(conn.execute(
                "SELECT total_festas, classificacao, ultima_festa"
                " FROM clientes WHERE id=?", (cid,)).fetchone())
        assert c["total_festas"] == 3
        assert c["classificacao"] == "Cliente recorrente"
        assert c["ultima_festa"] == "2024-03-15"

    def test_nao_conta_nao_entregues(self, app):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 300, "origem": "Formulario Festas",
            "data_evento": "2024-06-15", "status_origem": "entregue",
        })
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 301, "origem": "Formulario Festas",
            "data_evento": "2026-10-15", "status_origem": "aprovado",
        })
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 302, "origem": "Formulario Festas",
            "data_evento": "2024-08-15", "status_origem": "cancelado",
        })
        dados.atualizar_classificacao_cliente(cid)
        with dados.conectar() as conn:
            c = dict(conn.execute(
                "SELECT total_festas FROM clientes WHERE id=?",
                (cid,)).fetchone())
        assert c["total_festas"] == 1

    def test_atualizar_todas_classificacoes(self, app):
        _vincular_clientes()
        with dados.conectar() as conn:
            c1 = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
            c2 = conn.execute(
                "SELECT id FROM clientes WHERE nome='Joao Santos'").fetchone()[0]
        for i in range(5):
            dados.salvar_evento_historico({
                "cliente_id": c1, "origem_id": 400 + i, "origem": "Formulario Festas",
                "data_evento": f"2024-0{i+1}-15",
                "status_origem": "entregue",
            })
        dados.salvar_evento_historico({
            "cliente_id": c2, "origem_id": 450, "origem": "Formulario Festas",
            "data_evento": "2024-06-15", "status_origem": "entregue",
        })
        dados.atualizar_todas_classificacoes()
        with dados.conectar() as conn:
            maria = dict(conn.execute(
                "SELECT total_festas, classificacao FROM clientes WHERE id=?",
                (c1,)).fetchone())
            joao = dict(conn.execute(
                "SELECT total_festas, classificacao FROM clientes WHERE id=?",
                (c2,)).fetchone())
        assert maria["total_festas"] == 5
        assert maria["classificacao"] == "Cliente frequente"
        assert joao["total_festas"] == 1
        assert joao["classificacao"] == "Cliente de 1 festa"


class TesteImportarEventos3D:
    @pytest.fixture(autouse=True)
    def setup_3d(self, app, tmp_path):
        self.db_3d = str(tmp_path / "sistema.sqlite3")
        self.conn_3d = _criar_banco_3d(self.db_3d)
        _popular_3d(self.conn_3d)
        _vincular_clientes()
        yield
        self.conn_3d.close()

    def _importar(self):
        from ferramentas.importar_eventos_3d import importar, atualizar_classificacoes
        conn_festas = dados.conectar()
        try:
            r = importar(conn_festas, self.conn_3d)
            c = atualizar_classificacoes(conn_festas)
            r.update(c)
            return r
        finally:
            conn_festas.close()

    def test_importa_exceto_orcamento(self):
        r = self._importar()
        assert r["total_3d"] == 6
        assert r["importados"] == 5

    def test_ignora_orcamento(self):
        r = self._importar()
        with dados.conectar() as conn:
            orc = conn.execute(
                "SELECT * FROM eventos_historico"
                " WHERE status_origem = 'orcamento'").fetchall()
        assert len(orc) == 0

    def test_sem_correspondencia_para_revisao(self):
        r = self._importar()
        assert len(r["sem_correspondencia"]) >= 1
        pendente = r["sem_correspondencia"][0]
        assert pendente["cliente_3d_nome"] == "Sem Vinculo"
        assert pendente["cliente_3d_id"] == 3

    def test_idempotencia(self):
        r1 = self._importar()
        assert r1["importados"] == 5
        r2 = self._importar()
        assert r2["importados"] == 0
        assert r2["ignorados_duplicados"] == 5

    def test_3d_fica_no_banco_mas_nao_conta_como_festa(self):
        self._importar()
        with dados.conectar() as conn:
            total_3d = conn.execute("SELECT COUNT(*) FROM eventos_historico"
                                    " WHERE origem='Morumbi 3D'").fetchone()[0]
            clientes = [dict(r) for r in conn.execute(
                "SELECT total_festas, classificacao FROM clientes"
                " WHERE nome IN ('Maria Silva', 'Joao Santos')")]
        assert total_3d == 5
        assert all(c["total_festas"] == 0 for c in clientes)
        assert all(c["classificacao"] == "Sem histórico" for c in clientes)

    def test_vincula_por_nome(self):
        """Cliente sem cliente_3d_id mas com nome igual e vinculado."""
        with dados.conectar() as conn:
            conn.execute(
                "UPDATE clientes SET cliente_3d_id = NULL"
                " WHERE nome = 'Maria Silva'")
        r = self._importar()
        assert r["vinculados_por_nome"] >= 1
        with dados.conectar() as conn:
            maria = conn.execute(
                "SELECT cliente_3d_id FROM clientes"
                " WHERE nome='Maria Silva'").fetchone()
            assert maria["cliente_3d_id"] == 1

    def test_eventos_3d_nao_aparecem_na_agenda(self):
        self._importar()
        assert dados.eventos_historico("2024-01-01", "2025-12-31") == []

    def test_evento_nao_cria_pedido(self):
        self._importar()
        with dados.conectar() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM pedidos").fetchone()[0]
        assert total == 0

    def test_evento_nao_cria_reserva(self):
        self._importar()
        with dados.conectar() as conn:
            tabelas = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        if "reservas" in tabelas:
            with dados.conectar() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM reservas").fetchone()[0]
            assert total == 0


class TesteAgendaComHistorico:
    def test_dashboard_com_historico(self, app, admin):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 900,
            "data_evento": "2024-06-15", "descricao": "Festa",
            "status_origem": "entregue",
            "origem": "Formulario Festas",
        })
        r = admin.get("/?ano=2024&mes=6")
        assert r.status_code == 200

    def test_agenda_mensal_com_historico(self, app, admin):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 901,
            "data_evento": "2024-06-15", "descricao": "Festa Junina",
            "status_origem": "entregue", "origem": "Formulario Festas",
        })
        r = admin.get("/agenda?visao=mensal&ano=2024&mes=6")
        assert r.status_code == 200
        assert "Histórico" in r.text

    def test_agenda_diaria_com_historico(self, app, admin):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 902,
            "data_evento": "2024-06-15", "descricao": "Festa Junina",
            "status_origem": "entregue", "origem": "Formulario Festas",
            "valor": 500.0,
        })
        r = admin.get("/agenda?visao=diaria&ano=2024&mes=6&dia=15")
        assert r.status_code == 200
        assert "Histórico importado" in r.text
        assert "Maria Silva" in r.text

    def test_agenda_semanal_com_historico(self, app, admin):
        _vincular_clientes()
        with dados.conectar() as conn:
            cid = conn.execute(
                "SELECT id FROM clientes WHERE nome='Maria Silva'").fetchone()[0]
        dados.salvar_evento_historico({
            "cliente_id": cid, "origem_id": 903,
            "data_evento": "2024-06-15", "descricao": "Festa",
            "status_origem": "entregue", "origem": "Formulario Festas",
        })
        r = admin.get("/agenda?visao=semanal&ano=2024&mes=6&dia=15")
        assert r.status_code == 200
