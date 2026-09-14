"""Ordenacao e busca para listas do sistema."""

from unicodedata import normalize


def _normalizar(texto: str) -> str:
    return normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()


def filtrar(lista: list, busca: str, campos: tuple) -> list:
    if not busca:
        return lista
    termos = _normalizar(busca).split()
    resultado = []
    for item in lista:
        texto = " ".join(_normalizar(str(item.get(c, "") or "")) for c in campos)
        if all(t in texto for t in termos):
            resultado.append(item)
    return resultado


def ordenar(lista: list, chave: str, permitidos: tuple, inverter: bool = False) -> list:
    if not chave:
        return lista
    for nome, _, fn in permitidos:
        if nome == chave:
            return sorted(lista, key=lambda x: fn(x), reverse=inverter)
    return lista


ORDENS_USUARIOS = (
    ("nome", "Nome", lambda u: (u.get("nome") or "").lower()),
    ("perfil", "Perfil", lambda u: (u.get("perfil") or "").lower()),
    ("login", "Login", lambda u: (u.get("login") or "").lower()),
)

BUSCA_USUARIOS = ("nome", "login", "perfil")

ORDENS_CLIENTES = (
    ("nome", "Nome", lambda c: (c.get("nome") or "").lower()),
    ("cidade", "Cidade", lambda c: (c.get("cidade") or "").lower()),
    ("criado_em", "Data cadastro", lambda c: c.get("criado_em") or ""),
)

BUSCA_CLIENTES = ("nome", "whatsapp", "email", "cpf_cnpj", "cidade", "bairro", "instagram")

ORDENS_PRODUTOS = (
    ("nome", "Nome", lambda p: (p.get("nome") or "").lower()),
    ("codigo_sku", "SKU", lambda p: (p.get("codigo_sku") or "").lower()),
    ("preco_locacao", "Preco", lambda p: p.get("preco_locacao") or 0),
    ("quantidade_total", "Quantidade", lambda p: p.get("quantidade_total") or 0),
)

BUSCA_PRODUTOS = ("nome", "codigo_sku", "descricao", "localizacao", "categoria_nome")

ORDENS_KITS = (
    ("nome", "Nome", lambda k: (k.get("nome") or "").lower()),
    ("preco", "Preco", lambda k: k.get("preco") or 0),
)

BUSCA_KITS = ("nome", "descricao")

ORDENS_LEADS = (
    ("cliente_nome", "Cliente", lambda l: (l.get("cliente_nome") or "").lower()),
    ("valor_estimado", "Valor", lambda l: l.get("valor_estimado") or 0),
    ("atualizado_em", "Atualizado", lambda l: l.get("atualizado_em") or ""),
)

BUSCA_LEADS = ("cliente_nome", "interesse", "origem_nome", "observacoes")

ORDENS_ORCAMENTOS = (
    ("cliente_nome", "Cliente", lambda o: (o.get("cliente_nome") or "").lower()),
    ("total", "Total", lambda o: o.get("total") or 0),
    ("criado_em", "Data", lambda o: o.get("criado_em") or ""),
)

BUSCA_ORCAMENTOS = ("cliente_nome", "observacoes")

ORDENS_PEDIDOS = (
    ("cliente_nome", "Cliente", lambda p: (p.get("cliente_nome") or "").lower()),
    ("total", "Total", lambda p: p.get("total") or 0),
    ("criado_em", "Data", lambda p: p.get("criado_em") or ""),
)

BUSCA_PEDIDOS = ("cliente_nome", "observacoes")
