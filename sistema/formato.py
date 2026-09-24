"""Filtros Jinja2 para formatacao de valores."""

from datetime import date, datetime, timezone, timedelta

# Horário de Campo Grande: usado quando o fuso da empresa não pode ser lido.
FUSO = timezone(timedelta(hours=-4))
_provedor_fuso = None


def definir_provedor_fuso(funcao):
    """A camada de dados informa o fuso da empresa atual (configuração)."""
    global _provedor_fuso
    _provedor_fuso = funcao


def fuso():
    if _provedor_fuso:
        try:
            return _provedor_fuso() or FUSO
        except Exception:
            return FUSO
    return FUSO

MESES = (
    "", "janeiro", "fevereiro", "marco", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


def agora() -> str:
    return datetime.now(fuso()).strftime("%Y-%m-%dT%H:%M:%S")


def dinheiro(valor) -> str:
    if valor is None:
        return "—"
    v = float(valor)
    if v == 0:
        return "R$ 0,00"
    sinal = "− " if v < 0 else ""
    v = abs(v)
    inteiro = int(v)
    centavos = round((v - inteiro) * 100)
    milhar = f"{inteiro:,}".replace(",", ".")
    return f"{sinal}R$ {milhar},{centavos:02d}"


def ler_dinheiro(texto) -> float | None:
    """'1.250,50', 'R$ 150' ou '150.5' -> float; vazio -> None."""
    s = (texto or "").replace("R$", "").replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        valor = float(s)
    except ValueError:
        raise ValueError(f"Valor inválido: {texto}")
    if valor < 0:
        raise ValueError("O valor não pode ser negativo.")
    return round(valor, 2)


def numero(valor, casas=0) -> str:
    if valor is None:
        return "—"
    v = float(valor)
    if casas == 0:
        return f"{int(v):,}".replace(",", ".")
    fmt = f"{{:,.{casas}f}}"
    return fmt.format(v).replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_data(valor) -> str:
    if not valor:
        return "—"
    if isinstance(valor, str):
        valor = valor[:10]
        try:
            valor = date.fromisoformat(valor)
        except ValueError:
            return valor
    return valor.strftime("%d/%m/%Y")


def fmt_datahora(valor) -> str:
    if not valor:
        return "—"
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor[:19]).replace(tzinfo=timezone.utc)
        except ValueError:
            return valor
    return valor.astimezone(fuso()).strftime("%d/%m/%Y %H:%M")


def mes_por_extenso(ref=None) -> str:
    if ref is None:
        ref = date.today()
    return MESES[ref.month].capitalize()


def whatsapp_link(numero) -> str:
    if not numero:
        return ""
    n = "".join(c for c in str(numero) if c.isdigit() or c == "+")
    if not n:
        return ""
    if not n.startswith("55"):
        n = "55" + n
    return f"https://wa.me/{n}"


def registrar(app):
    app.jinja_env.filters["dinheiro"] = dinheiro
    app.jinja_env.filters["numero"] = numero
    app.jinja_env.filters["data"] = fmt_data
    app.jinja_env.filters["datahora"] = fmt_datahora
    app.jinja_env.filters["whatsapp_link"] = whatsapp_link
