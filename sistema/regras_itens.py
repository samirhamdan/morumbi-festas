"""Regras comerciais dos itens (Sprint 5.1), sem acesso ao banco.

Tipos de item, unidades de cobrança, quantidades fracionadas, consumo de
materiais, preço de referência de kits e modalidades de atendimento. Os
cálculos oficiais ficam aqui e são usados pelo backend, pela API e pelas
telas — o navegador só exibe.
"""

import math

# Tipo vazio (None) = "a classificar": produto cadastrado antes do 5.1, que
# continua se comportando como locação até alguém confirmar o tipo.
TIPOS_ITEM = {"locacao": "Locação", "encomenda": "Sob encomenda", "servico": "Serviço"}
ROTULO_A_CLASSIFICAR = "A classificar"

# unidade: (rótulo, sigla, aceita fração)
UNIDADES = {
    "unidade": ("Unidade", "un", False),
    "metro": ("Metro", "m", True),
    "servico": ("Serviço", "serv.", False),
    "hora": ("Hora", "h", True),
    "pacote": ("Pacote", "pct", False),
}
UNIDADES_POR_TIPO = {
    None: ("unidade",),
    "locacao": ("unidade",),
    "encomenda": ("unidade", "metro"),
    "servico": ("servico", "hora", "pacote"),
}
UNIDADE_PADRAO = {None: "unidade", "locacao": "unidade", "encomenda": "unidade",
                  "servico": "servico"}

MODALIDADES = {"retirada": "Retirada pelo cliente", "montagem": "Montagem no local"}
LOCAIS_EXECUCAO = {"evento": "No local do evento", "loja": "Na Morumbi Festas"}

UNIDADES_MATERIAL = ("un", "m", "cm", "kg", "g", "l", "ml", "pct", "rolo", "folha")
ARREDONDAMENTOS = {"exato": "Exato (aceita fração)",
                   "inteiro_acima": "Arredondar para cima (unidades inteiras)"}

CASAS = 3  # precisão das quantidades (milésimos de metro, de hora, de material)
QTD_MAXIMA = 9999


class ErroDeCampo(ValueError):
    def __init__(self, campo: str, mensagem: str):
        self.campo = campo
        super().__init__(mensagem)


def tipos_da_unidade(unidade) -> str:
    """Tipos que aceitam a unidade, separados por espaço (para a tela filtrar)."""
    return " ".join(t for t, us in UNIDADES_POR_TIPO.items() if t and unidade in us)


def consome_estoque(tipo) -> bool:
    """Só locação (e o que ainda não foi classificado) ocupa estoque físico."""
    return tipo in (None, "", "locacao")


def rotulo_tipo(tipo) -> str:
    return TIPOS_ITEM.get(tipo or "", ROTULO_A_CLASSIFICAR)


def sigla(unidade) -> str:
    return UNIDADES.get(unidade or "unidade", UNIDADES["unidade"])[1]


def aceita_fracao(unidade) -> bool:
    return UNIDADES.get(unidade or "unidade", UNIDADES["unidade"])[2]


def numero(valor, campo: str = "quantidade", rotulo: str = "Quantidade") -> float:
    """Converte texto do formulário ('2,5', '1.000,50', 3) em número."""
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        n = float(valor)
    else:
        texto = str(valor if valor is not None else "").strip().replace(" ", "")
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        try:
            n = float(texto)
        except ValueError:
            raise ErroDeCampo(campo, f"{rotulo} inválida.")
    if n != n or n in (math.inf, -math.inf):
        raise ErroDeCampo(campo, f"{rotulo} inválida.")
    return n


def quantidade(valor, unidade="unidade", campo="quantidade", minimo=None) -> float | int:
    """Quantidade válida para a unidade: inteira em 'unidade', 'serviço' e
    'pacote'; com até 3 casas em 'metro' e 'hora'. Devolve int quando inteira."""
    n = round(numero(valor, campo), CASAS)
    if n <= 0:
        raise ErroDeCampo(campo, "A quantidade precisa ser maior que zero.")
    if n > QTD_MAXIMA:
        raise ErroDeCampo(campo, f"Quantidade acima do limite ({QTD_MAXIMA}).")
    if not aceita_fracao(unidade) and n != int(n):
        raise ErroDeCampo(campo, f"Este item é cobrado por {UNIDADES.get(unidade or 'unidade')[0].lower()}"
                               " e não aceita quantidade fracionada.")
    if minimo and n < minimo:
        raise ErroDeCampo(campo, f"Quantidade mínima: {formatar_qtd(minimo)} {sigla(unidade)}.")
    return int(n) if n == int(n) else n


def formatar_qtd(n) -> str:
    """2.5 -> '2,5'; 3.0 -> '3' (sem zeros à direita)."""
    if n is None or n == "":
        return ""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    texto = f"{round(n, CASAS):.{CASAS}f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def validar_tipo_unidade(tipo, unidade) -> str:
    if tipo not in (None, "", *TIPOS_ITEM):
        raise ErroDeCampo("tipo", "Tipo de item inválido.")
    unidade = unidade or UNIDADE_PADRAO[tipo or None]
    if unidade not in UNIDADES:
        raise ErroDeCampo("unidade", "Unidade de cobrança inválida.")
    if unidade not in UNIDADES_POR_TIPO[tipo or None]:
        permitidas = ", ".join(UNIDADES[u][0].lower() for u in UNIDADES_POR_TIPO[tipo or None])
        raise ErroDeCampo("unidade", f"{rotulo_tipo(tipo)} é cobrado por: {permitidas}.")
    return unidade


# --- Classificação dos produtos antigos ---------------------------------------
# Só sugestão: a confirmação é sempre de uma pessoa (tela "A classificar").
PALAVRAS_TIPO = {
    "encomenda": ("balao", "baloes", "guirlanda", "arco", "coluna de bal", "desconstruid",
                  "bubble", "personalizad"),
    "servico": ("montagem", "instalacao", "desmontagem", "frete", "entrega", "deslocamento",
                "mao de obra", "servico"),
}


def _sem_acento(texto) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def sugerir_tipo(nome, descricao="") -> tuple:
    """(tipo sugerido | 'ambiguo', motivo) pelo nome e descrição."""
    import re
    texto = _sem_acento(f"{nome} {descricao or ''}")
    achados = {tipo for tipo, palavras in PALAVRAS_TIPO.items()
               if any(re.search(r"\b" + re.escape(p), texto) for p in palavras)}
    if len(achados) > 1:
        return "ambiguo", "nome indica mais de um tipo: " + ", ".join(sorted(achados))
    if achados:
        return achados.pop(), "palavra-chave no nome/descrição"
    return "locacao", "sem palavra-chave (o cadastro atual é de locação)"


# --- Materiais -----------------------------------------------------------------

def consumo_previsto(receita: list, qtd_vendida) -> list:
    """Materiais para produzir qtd_vendida (na unidade de cobrança do produto).

    receita: [{material_id, nome, unidade, quantidade (por 1 unidade de
    cobrança), arredondamento, custo_referencia}]. Proporcional à quantidade;
    só arredonda os materiais marcados como 'inteiro_acima'.
    """
    qtd_vendida = numero(qtd_vendida)
    saida = []
    for linha in receita:
        bruto = round(float(linha["quantidade"]) * qtd_vendida, 6)
        if linha.get("arredondamento") == "inteiro_acima":
            previsto = math.ceil(bruto - 1e-9)
        else:
            previsto = round(bruto, CASAS)
        custo = linha.get("custo_referencia")
        saida.append({
            "material_id": linha["material_id"], "nome": linha.get("nome", ""),
            "unidade": linha.get("unidade", ""), "por_unidade": float(linha["quantidade"]),
            "calculado": round(bruto, CASAS), "previsto": previsto,
            "arredondamento": linha.get("arredondamento", "exato"),
            "custo": round(previsto * custo, 2) if custo is not None else None,
        })
    return saida


# --- Kits ----------------------------------------------------------------------

def preco_referencia(componentes: list) -> dict:
    """Soma dos componentes (preço atual × quantidade), obrigatórios e opcionais."""
    obrig = sum(round(float(c["preco"] or 0) * float(c["quantidade"]), 2)
                for c in componentes if c.get("obrigatorio", 1))
    opc = sum(round(float(c["preco"] or 0) * float(c["quantidade"]), 2)
              for c in componentes if not c.get("obrigatorio", 1))
    return {"obrigatorios": round(obrig, 2), "opcionais": round(opc, 2),
            "total": round(obrig + opc, 2)}


def preco_final_kit(modo: str, preco_fechado, componentes: list) -> float:
    if modo == "componentes":
        return preco_referencia(componentes)["obrigatorios"]
    return round(float(preco_fechado or 0), 2)


def diferenca(referencia: float, final: float) -> dict | None:
    """Desconto (ou acréscimo) do preço final sobre a referência."""
    if not referencia:
        return None
    valor = round(referencia - final, 2)
    return {"valor": valor, "percentual": round(valor / referencia * 100, 1),
            "desconto": valor > 0}


# --- Modalidades -----------------------------------------------------------------

def modalidades_do_produto(p: dict) -> set:
    m = set()
    if p.get("permite_retirada", 1):
        m.add("retirada")
    if p.get("permite_montagem", 1):
        m.add("montagem")
    # serviço executado no evento não existe na retirada
    if p.get("tipo") == "servico" and p.get("local_execucao") == "evento":
        m.discard("retirada")
    return m


def modalidades_do_kit(kit: dict, componentes: list) -> tuple:
    """(modalidades possíveis, restrições) — o kit só oferece o que todos os
    componentes obrigatórios aceitam."""
    possiveis = {m for m in MODALIDADES if kit.get(f"permite_{m}", 1)}
    restricoes = []
    for c in componentes:
        if not c.get("obrigatorio", 1):
            continue
        aceitas = modalidades_do_produto(c)
        for m in sorted(possiveis - aceitas):
            restricoes.append(f"'{c.get('nome', 'componente')}' não permite {MODALIDADES[m].lower()}.")
        possiveis &= aceitas
    return possiveis, restricoes


def validar_modalidade(modalidade: str, permitidas: set, nome: str):
    if not modalidade:
        return
    if modalidade not in MODALIDADES:
        raise ErroDeCampo("modalidade", "Modalidade de atendimento inválida.")
    if modalidade not in permitidas:
        raise ErroDeCampo("modalidade", f"'{nome}' não permite {MODALIDADES[modalidade].lower()}.")
