"""Testes da camada de dados do Dashboard e do endpoint /api/painel."""

from datetime import date, timedelta

from sistema import dados, formato


def _hoje():
    return date.fromisoformat(formato.agora()[:10])


def _cliente(admin, nome="Ana Souza"):
    admin.post("/cliente", data={"nome": nome, "whatsapp": ""},
               follow_redirects=True)
    return [c for c in dados.listar_clientes() if c["nome"] == nome][0]


def _pedido(admin, cliente_id, **extra):
    futuro = (_hoje() + timedelta(days=30)).isoformat()
    agora = formato.agora()
    with dados.conectar() as conn:
        pid = conn.execute(
            "INSERT INTO pedidos (cliente_id, data_evento, data_retirada,"
            " data_devolucao, status_comercial, status_operacional,"
            " criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?)",
            (cliente_id,
             extra.get("data_evento", futuro),
             extra.get("data_retirada", futuro),
             extra.get("data_devolucao", futuro),
             extra.get("status_comercial", "confirmado"),
             extra.get("status_operacional", "preparacao"),
             agora, agora)).lastrowid
        conn.execute(
            "INSERT INTO itens_pedido (pedido_id, tipo, descricao,"
            " quantidade, preco_unitario) VALUES (?, 'produto', ?, 2, 50.0)",
            (pid, "Mesa redonda"))
    return {"id": pid}


class TesteIndicadores:
    def test_pedidos_ativos_excluem_finalizados_e_cancelados(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        _pedido(admin, cli["id"], status_operacional="separado")
        _pedido(admin, cli["id"], status_operacional="conferido",
                status_comercial="devolvido")
        cancelado = _pedido(admin, cli["id"])
        dados.cancelar_pedido(cancelado["id"])
        ind = dados.indicadores_dashboard()
        assert ind["pedidos_ativos"] == 2
        assert ind["pedidos_em_preparacao"] == 1

    def test_eventos_hoje_e_do_mes(self, app, admin):
        cli = _cliente(admin)
        hoje = _hoje()
        _pedido(admin, cli["id"], data_evento=hoje.isoformat())
        outro_dia = hoje.replace(day=1 if hoje.day != 1 else 2)
        _pedido(admin, cli["id"], data_evento=outro_dia.isoformat())
        ind = dados.indicadores_dashboard()
        assert ind["eventos_hoje"] == 1
        assert ind["eventos_mes"] == 2

    def test_clientes_total_e_novos(self, app, admin):
        _cliente(admin, "Ana")
        _cliente(admin, "Bia")
        ind = dados.indicadores_dashboard()
        assert ind["clientes_total"] == 2
        assert ind["clientes_novos_mes"] == 2


class TesteFaturamentoAnual:
    def test_agrega_por_mes_com_mesmo_criterio_do_mensal(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], status_comercial="devolvido",
                status_operacional="conferido", data_devolucao="2026-03-10")
        _pedido(admin, cli["id"], status_comercial="devolvido",
                status_operacional="conferido", data_devolucao="2026-03-20")
        _pedido(admin, cli["id"], data_devolucao="2026-03-25")
        meses = dados.faturamento_anual(2026)
        assert len(meses) == 12
        marco = meses[2]
        assert marco["quantidade"] == 2
        assert marco["total"] == 200.0
        assert marco["total"] == dados.faturamento_mensal(2026, 3)["total"]
        assert meses[0]["total"] == 0.0


class TesteAgendaDoDia:
    def test_lista_retirada_evento_devolucao_de_hoje(self, app, admin):
        cli = _cliente(admin, "Juliana Mello")
        hoje = _hoje().isoformat()
        _pedido(admin, cli["id"], data_retirada=hoje)
        _pedido(admin, cli["id"], data_evento=hoje)
        _pedido(admin, cli["id"], data_devolucao=hoje)
        agenda = dados.agenda_do_dia()
        assert [a["tipo"] for a in agenda] == ["retirada", "evento", "devolucao"]
        assert agenda[0]["cliente_nome"] == "Juliana Mello"

    def test_ignora_cancelados(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"], data_evento=_hoje().isoformat())
        dados.cancelar_pedido(ped["id"])
        assert dados.agenda_do_dia() == []


class TesteEsteira:
    def test_agrupa_status_operacional_nas_etapas(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        _pedido(admin, cli["id"], status_operacional="separado")
        _pedido(admin, cli["id"], status_operacional="montado")
        _pedido(admin, cli["id"], status_operacional="entregue",
                status_comercial="entregue")
        _pedido(admin, cli["id"], status_operacional="conferido",
                status_comercial="devolvido")
        totais = {e["chave"]: e["total"] for e in dados.esteira_pedidos()}
        assert totais == {"confirmados": 1, "em_preparacao": 2,
                          "em_entrega": 1, "finalizados": 1}

    def test_limita_pedidos_por_etapa(self, app, admin):
        cli = _cliente(admin)
        for _ in range(3):
            _pedido(admin, cli["id"])
        etapa = dados.esteira_pedidos(limite_por_etapa=2)[0]
        assert etapa["total"] == 3
        assert len(etapa["pedidos"]) == 2


class TesteAlertas:
    def test_devolucao_atrasada(self, app, admin):
        cli = _cliente(admin)
        ontem = (_hoje() - timedelta(days=1)).isoformat()
        _pedido(admin, cli["id"], data_devolucao=ontem,
                status_operacional="entregue")
        _pedido(admin, cli["id"], data_devolucao=ontem,
                status_operacional="conferido", status_comercial="devolvido")
        alertas = {a["tipo"]: a for a in dados.alertas_dashboard()}
        assert alertas["devolucao_atrasada"]["quantidade"] == 1

    def test_orcamento_sem_retorno_respeita_dias(self, app, admin):
        cli = _cliente(admin)
        velho = (_hoje() - timedelta(days=10)).isoformat() + "T10:00:00"
        with dados.conectar() as conn:
            conn.execute(
                "INSERT INTO orcamentos (cliente_id, status, criado_em,"
                " atualizado_em) VALUES (?, 'enviado', ?, ?)",
                (cli["id"], velho, velho))
            conn.execute(
                "INSERT INTO orcamentos (cliente_id, status, criado_em,"
                " atualizado_em) VALUES (?, 'enviado', ?, ?)",
                (cli["id"], formato.agora(), formato.agora()))
        alertas = {a["tipo"]: a for a in dados.alertas_dashboard()}
        assert alertas["orcamento_sem_retorno"]["quantidade"] == 1
        dados.salvar_parametro("dias_orcamento_sem_retorno", "30")
        assert dados.alertas_dashboard() == []

    def test_sem_pendencias_retorna_vazio(self, app, admin):
        assert dados.alertas_dashboard() == []


class TesteApiPainel:
    def test_exige_login(self, client):
        r = client.get("/api/painel")
        assert r.status_code in (302, 401)

    def test_admin_recebe_todos_os_blocos(self, app, admin):
        r = admin.get("/api/painel?ano=2026")
        corpo = r.get_json()
        assert r.status_code == 200
        for chave in ("indicadores", "agenda_hoje", "esteira", "alertas",
                      "faturamento_mes", "faturamento_anual"):
            assert chave in corpo
        assert corpo["faturamento_anual"]["ano"] == 2026
        assert len(corpo["faturamento_anual"]["meses"]) == 12

    def test_operacional_nao_recebe_faturamento(self, app, admin, client):
        admin.post("/usuario", data={"nome": "Op", "login": "op",
                                     "senha": "op12345", "perfil": "operacional",
                                     "ativo": "1"}, follow_redirects=True)
        admin.get("/sair")
        client.post("/entrar", data={"login": "op", "senha": "op12345"})
        corpo = client.get("/api/painel").get_json()
        assert "faturamento_mes" not in corpo
        assert "faturamento_anual" not in corpo
        assert "indicadores" in corpo

    def test_alerta_traz_link(self, app, admin):
        cli = _cliente(admin)
        ontem = (_hoje() - timedelta(days=1)).isoformat()
        _pedido(admin, cli["id"], data_devolucao=ontem,
                status_operacional="entregue")
        alertas = admin.get("/api/painel").get_json()["alertas"]
        assert alertas[0]["link"] == "/operacao"
