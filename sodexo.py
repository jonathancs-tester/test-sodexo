import os
import sys
import json
import base64
import argparse
from datetime import datetime, timezone, date, timedelta
from typing import Optional, Dict, Any, List
import requests
import urllib3

urllib3.disable_warnings()
# Timezone local (Brasília); se não disponível, cai para localtime
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    TZ_BRA = ZoneInfo("America/Sao_Paulo")
except Exception:
    TZ_BRA = None

# =========================
# CONFIGURAÇÃO DO MENU
# =========================

RESTAURANT_UNIT_ID = "628baa50edd6ea837b43ba34"

LOGIN_URL = "https://api.wedigitek.io/auth/login"
MENUS_BY_DATE_URL = "https://api.wedigitek.io/restaurants/{unitId}/menus?availableFor={availableFor}&date={yyyy_mm_dd}"
MENU_BY_ID_URL = "https://api.wedigitek.io/restaurants/{unitId}/menus/{menuId}"

COMMON_HEADERS = {
    "Accept-Language": "en,en-US;q=0.9,pt-BR;q=0.8,pt;q=0.7",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/144.0.0.0 Safari/537.36",
    "Origin": "https://sodexodirect.com",
    "Referer": "https://sodexodirect.com/",
}

# ===== Webhook do Microsoft Teams =====
# Configure via variável de ambiente TEAMS_WEBHOOK_URL
TEAMS_WEBHOOK_URL = os.getenv(
    "TEAMS_WEBHOOK_URL",
     #"https://default1b5ba8a2315d45ce959a42b748c01d.e7.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/8e89150054b34d5f9f9d312711d1594e/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=gznsgi0YjFliS07puhr1PYiUUKjD5_EyVM0Fe-u2yW8"
   "https://default1b5ba8a2315d45ce959a42b748c01d.e7.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/6f873eb0ef374a699669e8533f06ff76/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=-azBxWgLjambz27TxWoB1lxCoXweREwdl_S3Hydv8f4"
)


# =========================
# FUNÇÕES DE SUPORTE
# =========================

def parse_args():
    parser = argparse.ArgumentParser(description="Busca cardápio do dia com login na Wedigitek e envia para Teams")
    parser.add_argument("--email", default=os.getenv("WE_EMAIL"), help="Email de login (ou WE_EMAIL env)")
    parser.add_argument("--password", default=os.getenv("WE_PASSWORD"), help="Senha (ou WE_PASSWORD env)")
    parser.add_argument("--client-id", default=os.getenv("WE_CLIENT_ID"), help="ClientId (ou WE_CLIENT_ID env)")
    parser.add_argument("--idioma", default="pt-BR", help="Idioma a exibir (default: pt-BR)")
    parser.add_argument("--available-for", default="IN", help="Filtro availableFor (ex.: IN, OUT). Default: IN")
    parser.add_argument(
        "--date",
        default=None,
        help="Data no formato YYYY-MM-DD. Se não informada, usa a data atual da máquina (TZ Brasília)."
    )
    parser.add_argument(
        "--fallback-days",
        type=int,
        default=0,
        help="Se não encontrar menu para a data informada, tente os próximos N dias. Default: 0 (não tenta)."
    )
    return parser.parse_args()


def hoje_yyyy_mm_dd(forced_date: Optional[str] = None) -> str:
    """
    Retorna a data no formato YYYY-MM-DD.
    Se forced_date for fornecida, valida e retorna.
    Caso contrário, pega a data atual na TZ de Brasília (ou localtime se indisponível).
    """
    if forced_date:
        try:
            dt = datetime.strptime(forced_date, "%Y-%m-%d").date()
            return dt.isoformat()
        except ValueError:
            raise ValueError("Parâmetro --date inválido. Use formato YYYY-MM-DD (ex.: 2026-01-22).")

    if TZ_BRA:
        return datetime.now(TZ_BRA).date().isoformat()
    return date.today().isoformat()


def jwt_exp_epoch(jwt_token: str):
    """
    (Opcional) Extrai o campo exp de um JWT sem validar assinatura,
    útil só para log/diagnóstico local.
    """
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        payload = json.loads(payload_json)
        return payload.get("exp")
    except Exception:
        return None


def login(email: str, password: str, client_id: str) -> str:
    """
    Realiza o POST /auth/login e retorna o valor de 'token' do JSON.
    Lança HTTPError para códigos não-2xx e ValueError se faltar a chave.
    """
    if not email or not password or not client_id:
        raise ValueError("Credenciais incompletas: informe email, password e clientId (via env ou argumentos).")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        **COMMON_HEADERS,
    }
    payload = {
        "email": email,
        "password": password,
        "clientId": client_id,
    }

    resp = requests.post(LOGIN_URL, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    token = data.get("token")
    if not token:
        raise ValueError("Resposta de login não contém a chave 'token'. JSON recebido: " + json.dumps(data)[:400])

    exp = jwt_exp_epoch(token)
    if exp:
        dt = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone()
        print(f"🔑 Token recebido. Expira em: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    return token


def get_menu_list_for_date(token: str, unit_id: str, available_for: str, yyyy_mm_dd: str) -> List[Dict[str, Any]]:
    """
    Chama GET /restaurants/{unitId}/menus?availableFor=IN&date=YYYY-MM-DD
    Retorna a lista de menus (conteúdo de 'docs' quando paginado).
    """
    url = MENUS_BY_DATE_URL.format(unitId=unit_id, availableFor=available_for, yyyy_mm_dd=yyyy_mm_dd)
    headers = {
        "Accept": "*/*",
        "Authorization": f"We {token}",
        **COMMON_HEADERS,
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    # Possíveis formatos: {docs: [...]}, {menus: [...]}, ou lista []
    if isinstance(data, dict):
        if isinstance(data.get("docs"), list):
            return data["docs"]
        if isinstance(data.get("menus"), list):
            return data["menus"]
    if isinstance(data, list):
        return data

    return []


def pick_menu_id(menus: List[Dict[str, Any]]) -> Optional[str]:
    """
    Seleciona um menuId da lista. Prioriza 'availability==true' e/ou 'enabled==true'.
    Aceita tanto 'id' quanto '_id'.
    """
    if not menus:
        return None

    def _get_id(m):
        return m.get("id") or m.get("_id")

    # 1) Disponíveis e habilitados
    for m in menus:
        if (m.get("availability") is True) and (m.get("enabled") is True):
            mid = _get_id(m)
            if mid:
                return mid

    # 2) Disponíveis OU habilitados
    for m in menus:
        if (m.get("availability") is True) or (m.get("enabled") is True):
            mid = _get_id(m)
            if mid:
                return mid

    # 3) Fallback: primeiro com id
    for m in menus:
        mid = _get_id(m)
        if mid:
            return mid

    return None


def get_menu_by_id(token: str, unit_id: str, menu_id: str) -> Dict[str, Any]:
    """
    Busca o cardápio detalhado com Authorization: We <token>.
    """
    url = MENU_BY_ID_URL.format(unitId=unit_id, menuId=menu_id)
    headers = {
        "Accept": "*/*",
        "Authorization": f"We {token}",
        **COMMON_HEADERS,
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def extrair_cardapio(json_cardapio: dict, idioma: str = "pt-BR"):
    """
    Transforma o JSON da API em uma estrutura simples para uso e exibição.
    """
    resultado = []
    for categoria in json_cardapio.get("categories", []):
        # aceita tanto 'categoryName' (obj) quanto 'name'
        nome_categoria = None
        cat_name_obj = categoria.get("categoryName") or categoria.get("name")
        if isinstance(cat_name_obj, dict):
            nome_categoria = (
                cat_name_obj.get(idioma)
                or cat_name_obj.get("pt-BR")
                or cat_name_obj.get("en")
                or "Sem nome"
            )
        else:
            nome_categoria = cat_name_obj or "Sem nome"

        produtos = []
        for item in categoria.get("products", []):
            prod = item.get("product", {}) or {}
            # name/description podem vir como dict por idioma ou texto direto
            name_obj = prod.get("name")
            if isinstance(name_obj, dict):
                nome = name_obj.get(idioma) or name_obj.get("pt-BR") or name_obj.get("en") or "Sem nome"
            else:
                nome = name_obj or "Sem nome"

            desc_obj = prod.get("description")
            if isinstance(desc_obj, dict):
                descricao = desc_obj.get(idioma) or desc_obj.get("pt-BR") or desc_obj.get("en") or ""
            else:
                descricao = desc_obj or ""

            produtos.append({
                "categoria": nome_categoria,
                "nome": nome,
                "alergenos": prod.get("allergens", []) or [],
                "descricao": descricao,
                "imagem": prod.get("imageUrl", "") or "",
                "tags": item.get("tags", []) or [],
            })
        if produtos:
            resultado.append({"categoria": nome_categoria, "produtos": produtos})
    return resultado


def montar_texto_para_console(cardapio):
    """
    Gera texto para console (e também enviaremos o mesmo para o Teams).
    """
    linhas = []
    linhas.append("\n========== CARDÁPIO ==========\n")
    for categoria in cardapio:
        linhas.append(f"🍽 {categoria['categoria']}")
        for produto in categoria["produtos"]:
            nome = produto["nome"]
            alergenos = ", ".join(produto["alergenos"]) if produto["alergenos"] else "Nenhum"
            linhas.append(f"  - {nome}")
            if produto.get("tags"):
                linhas.append(f"    Tags: {', '.join(produto['tags'])}")
            if produto.get("descricao"):
                linhas.append(f"    Descrição: {produto['descricao']}")
            linhas.append(f"    Alergênos: {alergenos}")
        linhas.append("")  # linha em branco
    return "\n".join(linhas)


def montar_texto_para_teams(cardapio, idioma="pt-BR", date_label: Optional[str] = None):
    """
    Gera um resumo legível no Teams usando apenas Markdown compatível com webhook.
    """
    hoje = date_label or datetime.now().strftime("%d/%m/%Y")
    total_produtos = sum(len(categoria.get("produtos", [])) for categoria in cardapio)
    linhas = [
        "🍽 <strong>Cardápio do dia</strong><br>",
        f"📅 <strong>Data:</strong> {hoje}<br>",
        # f"🍴 <strong>Opções disponíveis:</strong> {total_produtos}",
        "",
        "---",
        "",
    ]

    for categoria in cardapio:
        nome_categoria = str(categoria.get("categoria", "Sem nome")).strip()
        categoria_normalizada = nome_categoria.rstrip(" !:").casefold()
        if categoria_normalizada in {"aviso", "avisos", "notice"}:
            linhas.append("## ℹ️ Aviso")
            for produto in categoria.get("produtos", []):
                aviso = produto.get("nome", "").strip()
                descricao_aviso = produto.get("descricao", "").strip()
                texto_aviso = aviso or descricao_aviso
                if aviso and descricao_aviso:
                    texto_aviso = f"{aviso} {descricao_aviso}"
                if texto_aviso:
                    linhas.append(f"> {texto_aviso}")
            linhas.append("")
            linhas.append("---")
            linhas.append("")
            continue

        linhas.append(f"## {nome_categoria}")
        for produto in categoria["produtos"]:
            nome = produto["nome"]
            linhas.append(f"- <strong>{nome}</strong>")

            descricao = produto.get("descricao")
            if descricao:
                linhas.append(f"  - Descrição: {descricao}")

            tags = produto.get("tags") or []
            if tags:
                linhas.append(f"  🏷️ {', '.join(str(tag) for tag in tags)}")

            alergenos = produto.get("alergenos") or []
            alerta = ", ".join(str(alergeno) for alergeno in alergenos) if alergenos else "Nenhum informado"
            # linhas.append(f"  ⚠️ <strong>Alergênicos:</strong> {alerta}")

            linhas.append("")

        linhas.append("---")
        linhas.append("")

    linhas.append("_Bom apetite!_")
    return "\n".join(linhas)

def enviar_para_teams(texto_markdown: str):
    payload = {
        "text": texto_markdown
    }

    try:
        resp = requests.post(
            TEAMS_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20
        )

        print(f"📡 Status envio Teams: {resp.status_code}")

        # Debug útil (Power Automate retorna erros em texto)
        if resp.status_code >= 400:
            print(f"❌ Erro resposta: {resp.text[:500]}")

        resp.raise_for_status()

    except requests.exceptions.RequestException as e:
        print(f"❌ Falha ao enviar para Teams: {e}")
        raise

def resolver_menu_id_para_intervalo(token: str, unit_id: str, available_for: str, start_date: str, fallback_days: int):
    """
    Tenta obter um menuId para a data base (start_date). Se não encontrar, tenta os próximos N dias.
    Retorna (menu_id, data_utilizada:str) ou (None, ultima_data_tentada).
    """
    base_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    tentativas = [base_dt + timedelta(days=i) for i in range(fallback_days + 1)]

    for dt in tentativas:
        yyyy_mm_dd = dt.isoformat()
        menus = get_menu_list_for_date(token, unit_id, available_for, yyyy_mm_dd)
        print(f"🔎 {yyyy_mm_dd}: menus retornados = {len(menus)}")
        if menus:
            # Log resumido para depuração
            for i, m in enumerate(menus, 1):
                print(f"  {i:02d}) id={m.get('id') or m.get('_id')} "
                      f"name={m.get('name') or m.get('names',{}).get('pt-BR')} "
                      f"enabled={m.get('enabled')} availability={m.get('availability')}")
            menu_id = pick_menu_id(menus)
            if menu_id:
                return menu_id, yyyy_mm_dd
    # Se não achou, devolve None e a última data tentada
    return None, tentativas[-1].isoformat()


def main():
    args = parse_args()

    # Determina a data base
    yyyy_mm_dd = hoje_yyyy_mm_dd(args.date)
    date_label_br = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").strftime("%d/%m/%Y")

    try:
        print("🔐 Autenticando...")
        token = login(args.email, args.password, args.client_id)

        print(f"📅 Buscando menuId para a data base: {yyyy_mm_dd} (availableFor={args.available_for})...")
        menu_id, data_utilizada = resolver_menu_id_para_intervalo(
            token,
            RESTAURANT_UNIT_ID,
            args.available_for,
            yyyy_mm_dd,
            args.fallback_days,
        )

        if not menu_id:
            print("⚠️ Nenhum menu encontrado no intervalo solicitado.")
            sys.exit(0)

        print(f"🔎 menuId selecionado: {menu_id} (data: {data_utilizada})")
        print("🔄 Buscando cardápio detalhado...")
        menu_json = get_menu_by_id(token, RESTAURANT_UNIT_ID, menu_id)

        print("🔍 Processando...")
        cardapio = extrair_cardapio(menu_json, idioma=args.idioma)

        if not cardapio:
            print("⚠️ Cardápio vazio para o menu selecionado.")
            sys.exit(0)

        # Texto para console
        texto_console = montar_texto_para_console(cardapio)
        print(texto_console)

        # Texto para Teams
        # Ajusta o label para a data efetivamente usada
        date_label_br = datetime.strptime(data_utilizada, "%Y-%m-%d").strftime("%d/%m/%Y")
        texto_teams = montar_texto_para_teams(cardapio, idioma=args.idioma, date_label=date_label_br)

        print("📨 Enviando para Teams...")
        enviar_para_teams(texto_teams)

        print("✅ Mensagem enviada com sucesso ao Teams!")

    except requests.HTTPError as e:
        if e.response is not None:
            print(f"❌ HTTP {e.response.status_code} - {e.response.text[:800]}")
        else:
            print(f"❌ HTTPError: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Erro: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
