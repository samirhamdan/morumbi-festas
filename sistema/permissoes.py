"""Permissões por recurso e ação (Sprint 2.2).

Camada central de autorização: as rotas pedem uma permissão
(``auth.exige_permissao("orders.edit")``) e as telas consultam ``tem(...)``.
Nenhum módulo decide acesso pelo nome do perfil.

Os perfis abaixo são o padrão do sistema. O desenho permite, no futuro,
permissões ajustadas por empresa sem mudar as rotas.
"""

# chave, grupo, descrição
PERMISSOES = (
    ("dashboard.view", "Painel", "Ver o painel"),
    ("customers.view", "Clientes", "Ver clientes"),
    ("customers.create", "Clientes", "Cadastrar clientes"),
    ("customers.edit", "Clientes", "Editar clientes"),
    ("customers.export", "Clientes", "Exportar clientes"),
    ("leads.view", "Comercial", "Ver leads"),
    ("leads.edit", "Comercial", "Criar e editar leads"),
    ("quotes.view", "Comercial", "Ver orçamentos"),
    ("quotes.edit", "Comercial", "Criar, editar e converter orçamentos"),
    ("orders.view", "Pedidos", "Ver pedidos"),
    ("orders.create", "Pedidos", "Criar pedidos"),
    ("orders.edit", "Pedidos", "Editar pedidos"),
    ("orders.cancel", "Pedidos", "Cancelar pedidos"),
    ("orders.finish", "Pedidos", "Finalizar pedidos"),
    ("orders.note", "Pedidos", "Registrar ocorrências"),
    ("orders.admin", "Pedidos", "Correção administrativa de status e conversão de importados"),
    ("agenda.view", "Agenda", "Ver a agenda"),
    ("agenda.edit", "Agenda", "Alterar datas da agenda"),
    ("operation.view", "Operação", "Ver a esteira de pedidos"),
    ("operation.edit", "Operação", "Avançar etapas operacionais"),
    ("catalog.view", "Catálogo", "Ver produtos, kits e categorias"),
    ("catalog.edit", "Catálogo", "Cadastrar produtos, kits e categorias"),
    ("inventory.view", "Estoque", "Ver disponibilidade de estoque"),
    ("inventory.edit", "Estoque", "Ajustar quantidades de estoque"),
    ("reports.view", "Relatórios", "Ver faturamento e relatórios"),
    ("finance.edit", "Relatórios", "Corrigir valor e data de pedidos importados"),
    ("settings.view", "Configurações", "Ver configurações da empresa"),
    ("settings.edit", "Configurações", "Alterar configurações da empresa"),
    ("users.view", "Usuários", "Ver usuários"),
    ("users.manage", "Usuários", "Gerenciar usuários e perfis"),
)
TODAS = frozenset(p[0] for p in PERMISSOES)

_BASE = {"dashboard.view", "catalog.view", "agenda.view", "inventory.view"}

# Acesso padrão de cada perfil. Os quatro perfis existentes mantêm exatamente
# o acesso que já tinham; Financeiro e Visualização são novos.
PERFIL_PERMISSOES = {
    "admin": TODAS,
    "gestor": frozenset(_BASE | {
        "orders.view", "orders.finish", "orders.note",
        "operation.view", "operation.edit", "reports.view", "finance.edit"}),
    "comercial": frozenset(_BASE | {
        "customers.view", "customers.create", "customers.edit", "customers.export",
        "leads.view", "leads.edit", "quotes.view", "quotes.edit",
        "orders.view", "orders.create", "orders.edit", "orders.cancel",
        "orders.note", "reports.view", "finance.edit"}),
    "operacional": frozenset(_BASE | {
        "orders.view", "orders.finish", "orders.note",
        "operation.view", "operation.edit"}),
    "financeiro": frozenset(_BASE | {
        "customers.view", "orders.view", "orders.note", "reports.view",
        "finance.edit"}),
    "visualizacao": frozenset(_BASE | {
        "customers.view", "leads.view", "quotes.view", "orders.view",
        "operation.view"}),
}

# Distinção para o futuro: dono da plataforma (usuarios.plataforma_admin)
# não é o administrador de uma empresa e ainda não tem telas próprias.
PAPEIS_PLATAFORMA = ("PLATFORM_ADMIN",)


def do_perfil(perfil: str | None) -> frozenset:
    return PERFIL_PERMISSOES.get(perfil or "", frozenset())


def matriz() -> list:
    """Linhas para a tela de permissões: grupo, descrição e perfis que têm."""
    return [{"chave": chave, "grupo": grupo, "descricao": descricao,
             "perfis": {p for p, perms in PERFIL_PERMISSOES.items() if chave in perms}}
            for chave, grupo, descricao in PERMISSOES]
