"""Envio de e-mail opcional (recuperação de senha).

Só funciona com um servidor SMTP configurado nas variáveis de ambiente:
FESTAS_SMTP_HOST, FESTAS_SMTP_PORTA (padrão 587), FESTAS_SMTP_USUARIO,
FESTAS_SMTP_SENHA e FESTAS_SMTP_REMETENTE, além de FESTAS_URL_BASE (endereço
público do sistema, ex.: https://morumbifestas.duckdns.org). Sem isso, nada é enviado e o
administrador gera o link pela tela de Usuários.
"""

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def configurado() -> bool:
    # O endereço público do site (FESTAS_URL_BASE) é obrigatório: o link do e-mail
    # nunca é montado a partir do endereço informado pela requisição.
    return bool(os.environ.get("FESTAS_SMTP_HOST") and os.environ.get("FESTAS_SMTP_REMETENTE")
                and os.environ.get("FESTAS_URL_BASE"))


def enviar(para: str, assunto: str, texto: str) -> bool:
    """True se entregue ao servidor SMTP. Falhas ficam só no log do sistema."""
    if not (configurado() and para):
        return False
    msg = EmailMessage()
    msg["From"] = os.environ["FESTAS_SMTP_REMETENTE"]
    msg["To"] = para
    msg["Subject"] = assunto
    msg.set_content(texto)
    porta = int(os.environ.get("FESTAS_SMTP_PORTA", "587"))
    try:
        classe = smtplib.SMTP_SSL if porta == 465 else smtplib.SMTP
        with classe(os.environ["FESTAS_SMTP_HOST"], porta, timeout=15) as smtp:
            if porta != 465:
                smtp.starttls()
            if os.environ.get("FESTAS_SMTP_USUARIO"):
                smtp.login(os.environ["FESTAS_SMTP_USUARIO"],
                           os.environ.get("FESTAS_SMTP_SENHA", ""))
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as e:
        log.warning("Falha ao enviar e-mail de recuperação: %s", e)
        return False
