"""Filtros Jinja2 para formatacao de valores."""

from datetime import date, datetime, timezone, timedelta

FUSO = timezone(timedelta(hours=-4))

MESES = (
    "", "janeiro", "fevereiro", "marco", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


def agora() -> str:
    return datetime.now(FUSO).strftime("%Y-%m-%dT%H:%M:%S")


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
    return valor.astimezone(FUSO).strftime("%d/%m/%Y %H:%M")


def mes_por_extenso(ref=None) -> str:
    if ref is None:
        ref = date.today()
    return MESES[ref.month].capitalize()


def registrar(app):
    app.jinja_env.filters["dinheiro"] = dinheiro
    app.jinja_env.filters["numero"] = numero
    app.jinja_env.filters["data"] = fmt_data
    app.jinja_env.filters["datahora"] = fmt_datahora
