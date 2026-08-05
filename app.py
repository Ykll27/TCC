import base64
import csv
import hashlib
import io
import json
import os
import statistics
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import qrcode
from flask import Flask, request, redirect, url_for, flash, render_template, send_file, Response, stream_with_context, jsonify, session, g
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from bd import iniciar_banco, conectar
from corretor import corrigir_imagem_web, ler_qrcode
from gerador_provas import gerar_prova_enem, _criar_questao_fallback


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
QR_DIR = BASE_DIR / "static" / "qrcodes"

UPLOAD_DIR.mkdir(exist_ok=True)
QR_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-troque-em-producao")
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024

iniciar_banco()


def professor_atual_id():
    return session.get("professor_id")


def professor_atual():
    pid = professor_atual_id()
    if not pid:
        return None
    conn = conectar()
    try:
        return conn.execute("SELECT * FROM professores WHERE id = ?", (int(pid),)).fetchone()
    finally:
        conn.close()


def existe_professor_cadastrado():
    conn = conectar()
    try:
        row = conn.execute("SELECT COUNT(*) AS total FROM professores WHERE senha_hash != ''").fetchone()
        return int(row["total"] or 0) > 0
    finally:
        conn.close()


ROTAS_PUBLICAS = {"login", "cadastro", "static"}


@app.before_request
def garantir_banco_atualizado():
    # Garante migrações e protege as rotas do painel.
    iniciar_banco()
    iniciar_worker_fila()
    g.professor = professor_atual()

    if request.endpoint in ROTAS_PUBLICAS or (request.endpoint or "").startswith("static"):
        return None

    if not existe_professor_cadastrado() and request.endpoint != "cadastro":
        return redirect(url_for("cadastro"))

    if not professor_atual_id():
        return redirect(url_for("login", proximo=request.path))

    return None


def gerar_qr(aluno, prova_id, titulo, disciplina, folha_codigo=None):
    """Gera QR Code compacto e fácil de ler.

    O modelo V1 de impressão usa também um identificador textual no rodapé
    (ex.: FLH-260801-ABC123). Esse mesmo código vai no QR para permitir
    validação cruzada e recuperação manual quando necessário.
    """
    aluno_id = int(aluno["id"])
    professor_id = int(professor_atual_id() or 0)
    folha_codigo = str(folha_codigo or gerar_codigo_folha(prova_id, aluno_id))
    assinatura = hashlib.sha256(
        f"atlas:{professor_id}:{aluno_id}:{prova_id}:{folha_codigo}".encode("utf-8")
    ).hexdigest()[:16]

    # Payload curto = QR mais limpo e mais legível por câmera.
    dados = {
        "v": 4,
        "s": "atlas",
        "f": folha_codigo,
        "p": int(prova_id),
        "a": aluno_id,
        "m": str(aluno["matricula"] or ""),
        "t": str(aluno["turma"] or ""),
        "sig": assinatura,
    }

    nome = f"prova_{prova_id}_aluno_{aluno_id}.png"
    caminho = QR_DIR / nome

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=5,
    )
    qr.add_data(json.dumps(dados, ensure_ascii=False, separators=(",", ":")))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    img.save(caminho)

    return nome


def _ordenar_mapa_questoes(mapa):
    """Ordena dicionários do tipo {'1': 'A', '2': 'C'} pela questão."""
    if not isinstance(mapa, dict):
        return {}

    def chave_ordem(item):
        chave, _ = item
        try:
            return int(chave)
        except Exception:
            return 10**9

    return dict(sorted(mapa.items(), key=chave_ordem))


def formatar_gabarito(mapa):
    """Transforma o gabarito em texto fácil de conferir na tela."""
    mapa = _ordenar_mapa_questoes(mapa)
    partes = []
    for questao, resposta in mapa.items():
        partes.append(f"{questao}:{str(resposta).upper()}")
    return " | ".join(partes)


def carregar_json_seguro(texto, padrao=None):
    if padrao is None:
        padrao = {}
    try:
        return json.loads(texto or "{}")
    except Exception:
        return padrao



def agora_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def gerar_codigo_folha(prova_id=None, aluno_id=None):
    """Gera um identificador textual no padrão do novo modelo V1 de impressão.

    O código aparece no rodapé da folha e também entra no QR.
    Ele serve como plano B para identificação manual quando o QR não for lido.
    """
    data = datetime.now().strftime("%y%m%d")
    base = f"{data}-{prova_id or uuid.uuid4().hex[:4]}-{aluno_id or uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:6]}"
    resumo = hashlib.sha1(base.encode("utf-8")).hexdigest()[:6].upper()
    return f"FLH-{data}-{resumo}"


def normalizar_termos(texto: str):
    partes = []
    for bruto in str(texto or "").replace(";", ",").replace("/", ",").split(","):
        termo = bruto.strip().lower()
        if len(termo) >= 3 and termo not in partes:
            partes.append(termo)
    return partes


def hash_questao(q):
    alternativas = q.get("alternativas", {}) if isinstance(q.get("alternativas"), dict) else {}
    base = json.dumps(
        {
            "enunciado": str(q.get("enunciado", "")).strip().lower(),
            "alternativas": {a: str(alternativas.get(a, "")).strip().lower() for a in ["A", "B", "C", "D", "E"]},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def buscar_questoes_cache(materias: str, temas: str, total: int, professor_id=None):
    """Busca questões já salvas para entregar prova rapidamente sem chamar IA."""
    termos_materia = normalizar_termos(materias)
    termos_tema = normalizar_termos(temas)
    total = max(1, int(total or 1))

    conn = conectar()
    try:
        condicoes = ["aprovado = 1"]
        params = []
        if professor_id:
            condicoes.append("(professor_id = ? OR professor_id IS NULL)")
            params.append(int(professor_id))

        if termos_materia or termos_tema:
            sub = []
            for termo in termos_materia:
                sub.append("LOWER(materia) LIKE ?")
                params.append(f"%{termo}%")
            for termo in termos_tema:
                sub.append("LOWER(tema) LIKE ?")
                params.append(f"%{termo}%")
            condicoes.append("(" + " OR ".join(sub) + ")")

        sql = f"""
            SELECT * FROM questoes_cache
            WHERE {' AND '.join(condicoes)}
            ORDER BY usado_vezes ASC, id DESC
            LIMIT ?
        """
        params.append(total)
        rows = conn.execute(sql, params).fetchall()

        if rows:
            ids = [int(r["id"]) for r in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE questoes_cache SET usado_vezes = usado_vezes + 1, atualizado_em = ? WHERE id IN ({placeholders})",
                [agora_str(), *ids],
            )
            conn.commit()

        questoes = []
        for i, row in enumerate(rows, start=1):
            alternativas = carregar_json_seguro(row["alternativas_json"], {})
            questoes.append(
                {
                    "numero": i,
                    "area": row["materia"],
                    "tema": row["tema"] or "",
                    "contexto": row["contexto"] or "",
                    "enunciado": row["enunciado"],
                    "alternativas": alternativas,
                    "correta": str(row["correta"] or "A").upper()[:1],
                    "habilidade": row["habilidade"] or "",
                    "explicacao": row["explicacao"] or "",
                    "origem_cache_id": row["id"],
                }
            )
        return questoes
    finally:
        conn.close()


def salvar_questoes_no_cache(prova_gerada, professor_id=None):
    """Salva questões boas no banco local para reduzir chamadas futuras de IA."""
    modo = str(prova_gerada.get("modo_geracao", "")).lower()
    if modo.startswith("fallback") or "fallback" in modo or "reserva" in modo:
        # Não salva questões demonstrativas/reserva para não poluir o banco.
        return 0

    questoes = prova_gerada.get("questoes", [])
    if not isinstance(questoes, list):
        return 0

    conn = conectar()
    salvas = 0
    try:
        for q in questoes:
            if not isinstance(q, dict):
                continue
            alternativas = q.get("alternativas", {}) if isinstance(q.get("alternativas"), dict) else {}
            if not str(q.get("enunciado", "")).strip() or not alternativas:
                continue
            h = hash_questao(q)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO questoes_cache
                    (professor_id, materia, tema, dificuldade, modelo, contexto, enunciado, alternativas_json, correta,
                     habilidade, explicacao, origem, hash, aprovado, usado_vezes, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (
                    int(professor_id) if professor_id else None,
                    str(q.get("area") or prova_gerada.get("materias") or "Conhecimentos gerais"),
                    str(q.get("tema") or prova_gerada.get("temas") or ""),
                    str(q.get("dificuldade") or "medio"),
                    "ENEM",
                    str(q.get("contexto") or ""),
                    str(q.get("enunciado") or ""),
                    json.dumps({a: str(alternativas.get(a, "")) for a in ["A", "B", "C", "D", "E"]}, ensure_ascii=False),
                    str(q.get("correta") or "A").upper()[:1],
                    str(q.get("habilidade") or ""),
                    str(q.get("explicacao") or ""),
                    modo or "ia",
                    h,
                    agora_str(),
                    agora_str(),
                ),
            )
            if cursor.rowcount:
                salvas += 1
        conn.commit()
        return salvas
    finally:
        conn.close()


def montar_prova_com_questoes(titulo, materias, temas, especificacoes, questoes, modo, aviso=""):
    questoes_prontas = []
    for i, q in enumerate(questoes, start=1):
        item = dict(q)
        item["numero"] = i
        questoes_prontas.append(item)
    gabarito = {str(q["numero"]): str(q.get("correta", "A")).upper()[:1] for q in questoes_prontas}
    return {
        "titulo": titulo,
        "materias": materias,
        "temas": temas,
        "total_questoes": len(questoes_prontas),
        "orientacoes": especificacoes,
        "questoes": questoes_prontas,
        "gabarito": gabarito,
        "modo_geracao": modo,
        "aviso": aviso,
    }


def gerar_prova_cache_ou_reserva(titulo, materias, temas, total_questoes, especificacoes, professor_id=None):
    """Modo rápido: tenta cache. Se não tiver, usa reserva local sem consumir IA."""
    cached = buscar_questoes_cache(materias, temas, total_questoes, professor_id=professor_id)
    if len(cached) >= total_questoes:
        return montar_prova_com_questoes(
            titulo, materias, temas, especificacoes, cached[:total_questoes], "cache_local",
            "Prova montada rapidamente usando questões já salvas no banco local.",
        )

    faltam = total_questoes - len(cached)
    novas = [
        _criar_questao_fallback(len(cached) + i, titulo, materias, temas)
        for i in range(1, faltam + 1)
    ]
    todas = [*cached, *novas][:total_questoes]
    modo = "rapido_cache_reserva" if cached else "rapido_reserva_local"
    aviso = (
        "Modo rápido: usei as questões disponíveis no banco local e completei o restante com questões reserva locais, sem consumir IA."
        if cached else "Modo rápido: ainda não havia questões suficientes no cache, então usei geração reserva local sem consumir IA."
    )
    return montar_prova_com_questoes(titulo, materias, temas, especificacoes, todas, modo, aviso)


def gerar_prova_hibrida(titulo, materias, temas, total_questoes, especificacoes, modo_solicitado="inteligente", professor_id=None, permitir_ia=False):
    """
    Geração segura para alta demanda:
    - rapido: prioriza cache/local;
    - inteligente: usa cache e chama IA só se faltar;
    - ia: força geração nova com IA/fallback.
    """
    total_questoes = max(1, min(int(total_questoes or 1), 90))
    modo_solicitado = (modo_solicitado or "inteligente").lower().strip()

    if modo_solicitado == "ia":
        return gerar_prova_enem(titulo, materias, temas, total_questoes, especificacoes)

    if modo_solicitado == "rapido":
        return gerar_prova_cache_ou_reserva(titulo, materias, temas, total_questoes, especificacoes, professor_id=professor_id)

    cached = buscar_questoes_cache(materias, temas, total_questoes, professor_id=professor_id)
    if len(cached) >= total_questoes:
        return montar_prova_com_questoes(
            titulo, materias, temas, especificacoes, cached[:total_questoes], "cache_local",
            "Prova montada em segundos usando o banco local de questões.",
        )

    faltam = total_questoes - len(cached)

    # Política ATLAS: usar o mínimo de IA possível.
    # Se o professor não autorizou IA, completa localmente para manter velocidade e evitar cota/429.
    if not permitir_ia:
        novas = [
            _criar_questao_fallback(len(cached) + i, titulo, materias, temas)
            for i in range(1, faltam + 1)
        ]
        todas = [*cached, *novas][:total_questoes]
        modo = "cache_reserva_sem_ia" if cached else "reserva_sem_ia"
        aviso = (
            f"Usei {len(cached)} questão(ões) do banco local e completei o restante sem consumir IA."
            if cached else "Prova criada em modo econômico, sem consumir IA. Ative a IA no formulário apenas quando precisar de questões mais originais/específicas."
        )
        return montar_prova_com_questoes(titulo, materias, temas, especificacoes, todas, modo, aviso)

    extras = especificacoes
    if cached:
        extras = (extras + "\n" if extras else "") + f"Gere {faltam} questão(ões) novas e diferentes das questões já usadas pelo banco local."

    prova_ia = gerar_prova_enem(titulo, materias, temas, faltam, extras)
    novas = prova_ia.get("questoes", []) if isinstance(prova_ia.get("questoes"), list) else []
    todas = [*cached, *novas][:total_questoes]

    modo_ia = str(prova_ia.get("modo_geracao", "ia"))
    if cached and not modo_ia.startswith("fallback"):
        modo = "misto_cache_ia"
        aviso = f"Usei {len(cached)} questão(ões) do banco local e IA para completar o restante."
    elif cached:
        modo = "misto_cache_fallback"
        aviso = f"Usei {len(cached)} questão(ões) do banco local e modo reserva para completar o restante."
    else:
        modo = modo_ia
        aviso = prova_ia.get("aviso", "")

    return montar_prova_com_questoes(titulo, materias, temas, especificacoes, todas, modo, aviso)


def inserir_avaliacao_gerada(prova_gerada, dados_pedido):
    gabarito = prova_gerada.get("gabarito", {})
    questoes = prova_gerada.get("questoes", [])

    conn = conectar()
    try:
        cursor = conn.execute(
            """
            INSERT INTO avaliacoes
                (professor_id, titulo, materias, temas, total_questoes, especificacoes, questoes_json, gabarito_json, status_revisao, criado_em, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'rascunho', ?, ?)
            """,
            (
                int(dados_pedido.get("professor_id") or professor_atual_id() or 1),
                prova_gerada.get("titulo", dados_pedido.get("titulo", "Simulado Modelo ENEM")),
                dados_pedido.get("materias", "Conhecimentos gerais"),
                dados_pedido.get("temas", ""),
                int(prova_gerada.get("total_questoes") or dados_pedido.get("total_questoes", 10)),
                dados_pedido.get("especificacoes", ""),
                json.dumps(questoes, ensure_ascii=False),
                json.dumps(gabarito, ensure_ascii=False),
                agora_str(),
                agora_str(),
            ),
        )
        avaliacao_id = cursor.lastrowid
        conn.commit()
        return avaliacao_id
    finally:
        conn.close()


_worker_iniciado = False
_worker_lock = threading.Lock()


def criar_tarefa_geracao(dados):
    conn = conectar()
    try:
        cursor = conn.execute(
            """
            INSERT INTO tarefas_ia (tipo, status, professor_id, dados_json, progresso, mensagem, criado_em, atualizado_em)
            VALUES ('gerar_prova', 'pendente', ?, ?, 0, ?, ?, ?)
            """,
            (
                int(dados.get("professor_id") or professor_atual_id() or 1),
                json.dumps(dados, ensure_ascii=False),
                "Pedido recebido. Aguardando processamento.",
                agora_str(),
                agora_str(),
            ),
        )
        tarefa_id = cursor.lastrowid
        conn.commit()
        return tarefa_id
    finally:
        conn.close()


def atualizar_tarefa(tarefa_id, status=None, progresso=None, mensagem=None, erro=None, resultado_id=None):
    campos = []
    params = []
    if status is not None:
        campos.append("status = ?")
        params.append(status)
    if progresso is not None:
        campos.append("progresso = ?")
        params.append(int(progresso))
    if mensagem is not None:
        campos.append("mensagem = ?")
        params.append(mensagem)
    if erro is not None:
        campos.append("erro = ?")
        params.append(erro)
    if resultado_id is not None:
        campos.append("resultado_id = ?")
        params.append(int(resultado_id))
    campos.append("atualizado_em = ?")
    params.append(agora_str())
    params.append(int(tarefa_id))

    conn = conectar()
    try:
        conn.execute(f"UPDATE tarefas_ia SET {', '.join(campos)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def carregar_tarefa(tarefa_id):
    conn = conectar()
    try:
        return conn.execute("SELECT * FROM tarefas_ia WHERE id = ?", (int(tarefa_id),)).fetchone()
    finally:
        conn.close()


def pegar_proxima_tarefa_pendente():
    conn = conectar()
    try:
        conn.execute("BEGIN IMMEDIATE")
        tarefa = conn.execute(
            """
            SELECT * FROM tarefas_ia
            WHERE status = 'pendente' AND tipo = 'gerar_prova'
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        if not tarefa:
            conn.commit()
            return None
        conn.execute(
            "UPDATE tarefas_ia SET status = 'processando', progresso = 5, mensagem = ?, atualizado_em = ? WHERE id = ?",
            ("Iniciando geração da prova.", agora_str(), int(tarefa["id"])),
        )
        conn.commit()
        return tarefa
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def processar_tarefa_geracao(tarefa):
    tarefa_id = int(tarefa["id"])
    try:
        dados = carregar_json_seguro(tarefa["dados_json"], {})
        atualizar_tarefa(tarefa_id, progresso=15, mensagem="Verificando banco local de questões e cache.")

        prova_gerada = gerar_prova_hibrida(
            titulo=dados.get("titulo", "Simulado Modelo ENEM"),
            materias=dados.get("materias", "Conhecimentos gerais"),
            temas=dados.get("temas", ""),
            total_questoes=int(dados.get("total_questoes", 10)),
            especificacoes=dados.get("especificacoes", ""),
            modo_solicitado="inteligente",
            professor_id=dados.get("professor_id"),
            permitir_ia=bool(dados.get("permitir_ia")),
        )

        atualizar_tarefa(tarefa_id, progresso=70, mensagem="Salvando prova, gabarito oficial e cache de questões.")
        avaliacao_id = inserir_avaliacao_gerada(prova_gerada, dados)
        salvas_cache = salvar_questoes_no_cache(prova_gerada, professor_id=dados.get("professor_id"))

        aviso = prova_gerada.get("aviso") or "Prova gerada com sucesso."
        mensagem = f"Concluído. Prova pronta. Questões novas salvas no cache: {salvas_cache}. {aviso}"

        atualizar_tarefa(
            tarefa_id,
            status="concluido",
            progresso=100,
            mensagem=mensagem,
            resultado_id=avaliacao_id,
        )
    except Exception as erro:
        atualizar_tarefa(
            tarefa_id,
            status="erro",
            progresso=100,
            mensagem="Não foi possível concluir a geração.",
            erro=str(erro),
        )


def worker_fila_ia():
    while True:
        tarefa = pegar_proxima_tarefa_pendente()
        if tarefa:
            processar_tarefa_geracao(tarefa)
        else:
            time.sleep(1)


def iniciar_worker_fila():
    global _worker_iniciado
    with _worker_lock:
        if _worker_iniciado:
            return
        t = threading.Thread(target=worker_fila_ia, daemon=True, name="worker-fila-ia")
        t.start()
        _worker_iniciado = True

def preparar_provas_para_tela(provas):
    """Adiciona o gabarito do professor já formatado nas provas."""
    provas_prontas = []
    for prova in provas:
        item = dict(prova)
        gabarito = carregar_json_seguro(item.get("gabarito_json"), {})
        item["gabarito_professor"] = _ordenar_mapa_questoes(gabarito)
        item["gabarito_formatado"] = formatar_gabarito(gabarito)
        provas_prontas.append(item)
    return provas_prontas


def preparar_resultados_para_tela(resultados):
    """Adiciona professor x aluno para tirar a prova real pela interface."""
    resultados_prontos = []
    for resultado in resultados:
        item = dict(resultado)
        bruto = carregar_json_seguro(item.get("resultado_json"), {})

        gabarito_professor = bruto.get("gabarito_professor_extraido", {})
        gabarito_aluno = bruto.get("gabarito_aluno_extraido", {})
        resumo = bruto.get("resultado", {})
        processamento = bruto.get("processamento", {})

        item["gabarito_professor"] = _ordenar_mapa_questoes(gabarito_professor)
        item["gabarito_aluno"] = _ordenar_mapa_questoes(gabarito_aluno)
        item["gabarito_professor_formatado"] = formatar_gabarito(gabarito_professor)
        item["gabarito_aluno_formatado"] = formatar_gabarito(gabarito_aluno)
        item["detalhes_questoes"] = resumo.get("detalhes", [])
        item["acertos"] = resumo.get("acertos", 0)
        item["erros"] = resumo.get("erros", 0)
        item["anuladas"] = resumo.get("anuladas_ou_em_branco", 0)
        item["total_questoes"] = resumo.get("total_questoes_oficial", 0)
        item["status_omr"] = (
            processamento.get("omr", {}).get("status_omr")
            if isinstance(processamento, dict)
            else None
        )
        item["erro_ia"] = (
            processamento.get("erro_ia") if isinstance(processamento, dict) else None
        )
        resultados_prontos.append(item)
    return resultados_prontos


def extensao_arquivo(nome_arquivo: str) -> str:
    """Retorna a extensão do arquivo em minúsculo, incluindo o ponto."""
    return Path(nome_arquivo or "").suffix.lower()


def salvar_upload_temporario(arquivo):
    """Salva um upload em disco com nome seguro."""
    nome_original = secure_filename(arquivo.filename or "folha.jpg")
    if not nome_original:
        nome_original = "folha.jpg"

    caminho = UPLOAD_DIR / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{nome_original}"
    arquivo.save(caminho)
    return caminho


def converter_pdf_para_imagens(caminho_pdf: Path):
    """
    Converte cada página de um PDF em JPG para o OpenCV conseguir corrigir.

    Requer PyMuPDF no requirements.txt:
        PyMuPDF
    """
    try:
        import fitz  # PyMuPDF
    except Exception as erro:
        raise RuntimeError(
            "Para corrigir PDF, instale a dependência PyMuPDF. Rode: pip install -r requirements.txt"
        ) from erro

    imagens = []
    documento = fitz.open(str(caminho_pdf))

    try:
        for indice, pagina in enumerate(documento, start=1):
            # 3x melhora a leitura do QR e das bolhas.
            # O QR antigo falhava em 2.5x em algumas folhas.
            matriz = fitz.Matrix(3.0, 3.0)
            pix = pagina.get_pixmap(matrix=matriz, alpha=False)
            caminho_img = UPLOAD_DIR / f"{caminho_pdf.stem}_pagina_{indice}.jpg"
            pix.save(str(caminho_img))
            imagens.append(caminho_img)
    finally:
        documento.close()

    return imagens


def preparar_arquivos_para_correcao(arquivo):
    """
    Recebe um upload do navegador e devolve uma lista de imagens prontas para corrigir.
    Imagem entra direto. PDF é convertido em imagens, uma por página.
    """
    caminho = salvar_upload_temporario(arquivo)
    extensao = extensao_arquivo(caminho.name)

    if extensao in [".jpg", ".jpeg", ".png", ".webp"]:
        return [caminho]

    if extensao == ".pdf":
        return converter_pdf_para_imagens(caminho)

    raise RuntimeError(
        f"Formato não aceito: {extensao or 'sem extensão'}. Envie PNG, JPG, WEBP ou PDF."
    )


def salvar_frame_base64(data_url: str) -> Path:
    """Recebe data:image/jpeg;base64,... do navegador e salva como JPG temporário."""
    if not data_url or not isinstance(data_url, str):
        raise RuntimeError("Nenhuma imagem foi recebida da câmera.")

    if "," in data_url:
        _, conteudo = data_url.split(",", 1)
    else:
        conteudo = data_url

    try:
        dados = base64.b64decode(conteudo, validate=True)
    except Exception as erro:
        raise RuntimeError("Imagem da câmera em formato inválido.") from erro

    if len(dados) < 5_000:
        raise RuntimeError("Imagem muito pequena. Aproxime a folha e tente novamente.")

    if len(dados) > 25_000_000:
        raise RuntimeError("Imagem muito grande. Reduza a resolução da câmera.")

    caminho = UPLOAD_DIR / f"scan_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:8]}.jpg"
    caminho.write_bytes(dados)
    return caminho


def buscar_prova_com_aluno(conn, prova_id: int):
    pid = professor_atual_id()
    if pid:
        return conn.execute(
            """
            SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
            FROM provas
            JOIN alunos ON alunos.id = provas.aluno_id
            WHERE provas.id = ? AND provas.professor_id = ?
            """,
            (int(prova_id), int(pid)),
        ).fetchone()
    return conn.execute(
        """
        SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
        FROM provas
        JOIN alunos ON alunos.id = provas.aluno_id
        WHERE provas.id = ?
        """,
        (int(prova_id),),
    ).fetchone()



def corrigir_folha_por_pipeline_unico(conn, caminho: Path, origem: str = "upload", criar_pendencia: bool = False, nome_arquivo: str = ""):
    """
    Pipeline único de correção usado por Upload e Scan.

    Regras:
    - QR Code é obrigatório para salvar automaticamente.
    - Se QR falhar, não usa prova/aluno base.
    - Scan só cria pendência manual quando criar_pendencia=True.
    - Upload usa o mesmo leitor de QR e o mesmo corretor de imagem.
    """
    dados_qr = ler_qrcode(str(caminho))

    if not dados_qr or not dados_qr.get("prova_id") or not dados_qr.get("aluno_id"):
        resposta = {
            "ok": False,
            "status": "qr_nao_lido" if criar_pendencia else "buscando_qr",
            "mensagem": "QR Code não lido. Aproxime a folha, melhore o foco e tente novamente.",
            "dados_qr": dados_qr or {},
            "pendencia_id": None,
            "pendencia_url": None,
        }
        if criar_pendencia:
            pendencia_id = salvar_pendencia_identificacao(
                conn,
                caminho,
                origem=origem,
                mensagem=f"QR Code não lido no {origem}. {nome_arquivo}".strip(),
            )
            resposta.update({
                "mensagem": "QR Code não lido. A folha foi enviada para Identificação Manual e nada foi salvo em aluno errado.",
                "pendencia_id": pendencia_id,
                "pendencia_url": url_for("resolver_identificacao_pendente", pendencia_id=pendencia_id),
            })
        return resposta

    prova_corrigir = buscar_prova_com_aluno(conn, int(dados_qr["prova_id"]))
    if not prova_corrigir:
        return {
            "ok": False,
            "status": "qr_invalido",
            "mensagem": "QR lido, mas a prova não existe ou pertence a outro professor. Nada foi salvo.",
            "dados_qr": dados_qr,
        }

    gabarito_oficial = json.loads(prova_corrigir["gabarito_json"])
    resultado = corrigir_imagem_web(
        caminho_gabarito_aluno=str(caminho),
        gabarito_oficial=gabarito_oficial,
        usar_ia="auto",
    )

    if dados_qr and not resultado.get("dados_qr"):
        resultado["dados_qr"] = dados_qr

    nota = float(resultado["resultado"].get("nota_percentual") or 0)
    processamento = resultado.get("processamento", {})
    omr = processamento.get("omr", {}) if isinstance(processamento, dict) else {}
    status_omr = str(omr.get("status_omr", ""))
    confianca_baixa = status_omr in ["falha_calibracao", "sem_marcacoes_detectadas", "erro"]
    status_leitura = "revisao" if confianca_baixa else "ok"
    mensagem = "QR lido com sucesso."
    if confianca_baixa:
        mensagem = f"{mensagem} Leitura OMR precisa de revisão: {omr.get('mensagem', status_omr)}"

    return {
        "ok": True,
        "status": status_leitura,
        "mensagem": mensagem,
        "dados_qr": dados_qr,
        "prova": prova_corrigir,
        "aluno_id": int(dados_qr["aluno_id"]),
        "prova_id": int(prova_corrigir["id"]),
        "nota": nota,
        "resultado": resultado,
    }

def recalcular_sessao_scan(conn, sessao_id: int):
    linhas = conn.execute(
        "SELECT status, latencia_ms FROM leituras_scan WHERE sessao_id = ?",
        (int(sessao_id),),
    ).fetchall()

    total = len(linhas)
    sucesso = sum(1 for l in linhas if l["status"] == "ok")
    revisao = total - sucesso
    tempos = [float(l["latencia_ms"] or 0) for l in linhas if l["latencia_ms"] is not None]
    tempo_medio = round(sum(tempos) / len(tempos), 2) if tempos else 0

    conn.execute(
        """
        UPDATE sessoes_scan
        SET total_processados = ?, total_sucesso = ?, total_revisao = ?, tempo_medio_ms = ?
        WHERE id = ?
        """,
        (total, sucesso, revisao, tempo_medio, int(sessao_id)),
    )

    return {
        "total_processados": total,
        "total_sucesso": sucesso,
        "total_revisao": revisao,
        "tempo_medio_ms": tempo_medio,
    }


def carregar_resumo_sessao_scan(sessao_id: int):
    conn = conectar()
    try:
        sessao = conn.execute("SELECT * FROM sessoes_scan WHERE id = ? AND professor_id = ?", (int(sessao_id), int(professor_atual_id()))).fetchone()
        if not sessao:
            return None
        leituras = conn.execute(
            """
            SELECT leituras_scan.*, alunos.nome AS aluno_nome, provas.titulo AS prova_titulo
            FROM leituras_scan
            LEFT JOIN alunos ON alunos.id = leituras_scan.aluno_id
            JOIN provas ON provas.id = leituras_scan.prova_id
            WHERE leituras_scan.sessao_id = ?
            ORDER BY leituras_scan.id DESC
            LIMIT 20
            """,
            (int(sessao_id),),
        ).fetchall()
        return {"sessao": sessao, "leituras": leituras}
    finally:
        conn.close()


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")

        if not nome or not email or len(senha) < 6:
            flash("Informe nome, e-mail e uma senha com pelo menos 6 caracteres.", "warning")
            return redirect(url_for("cadastro"))

        conn = conectar()
        try:
            existente = conn.execute("SELECT id FROM professores WHERE email = ?", (email,)).fetchone()
            if existente:
                flash("Esse e-mail já está cadastrado. Entre pelo login.", "warning")
                return redirect(url_for("login"))

            cursor = conn.execute(
                "INSERT INTO professores (nome, email, senha_hash, criado_em) VALUES (?, ?, ?, ?)",
                (nome, email, generate_password_hash(senha), agora_str()),
            )
            professor_id = cursor.lastrowid
            conn.commit()
            session["professor_id"] = professor_id
            flash("Conta criada. Bem-vindo ao Atlas.", "success")
            return redirect(url_for("home"))
        finally:
            conn.close()

    return render_template("login.html", modo="cadastro")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        conn = conectar()
        try:
            prof = conn.execute("SELECT * FROM professores WHERE email = ?", (email,)).fetchone()
            if prof and prof["senha_hash"] and check_password_hash(prof["senha_hash"], senha):
                session["professor_id"] = int(prof["id"])
                flash("Login realizado com sucesso.", "success")
                return redirect(request.args.get("proximo") or url_for("home"))
        finally:
            conn.close()
        flash("E-mail ou senha inválidos.", "danger")
        return redirect(url_for("login"))

    return render_template("login.html", modo="login")


@app.route("/logout")
def logout():
    session.clear()
    flash("Você saiu do Atlas.", "info")
    return redirect(url_for("login"))


@app.route("/")
def home():
    professor_id = int(professor_atual_id())
    conn = conectar()

    alunos = conn.execute("SELECT * FROM alunos WHERE professor_id = ? ORDER BY turma, nome", (professor_id,)).fetchall()

    avaliacoes = conn.execute(
        """
        SELECT * FROM avaliacoes
        WHERE professor_id = ?
        ORDER BY id DESC
    """,
        (professor_id,),
    ).fetchall()

    tarefas = conn.execute(
        """
        SELECT * FROM tarefas_ia
        WHERE professor_id = ?
        ORDER BY id DESC
        LIMIT 10
    """,
        (professor_id,),
    ).fetchall()

    provas = conn.execute(
        """
        SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
        FROM provas
        JOIN alunos ON alunos.id = provas.aluno_id
        WHERE provas.professor_id = ?
        ORDER BY provas.id DESC
    """,
        (professor_id,),
    ).fetchall()

    resultados = conn.execute(
        """
        SELECT resultados.*, alunos.nome AS aluno_nome, provas.titulo AS prova_titulo
        FROM resultados
        LEFT JOIN alunos ON alunos.id = resultados.aluno_id
        JOIN provas ON provas.id = resultados.prova_id
        WHERE resultados.professor_id = ?
        ORDER BY resultados.id DESC
    """,
        (professor_id,),
    ).fetchall()

    turmas = conn.execute("SELECT DISTINCT turma FROM alunos WHERE professor_id = ? ORDER BY turma", (professor_id,)).fetchall()

    conn.close()

    return render_template(
        "index.html",
        alunos=alunos,
        turmas=turmas,
        avaliacoes=avaliacoes,
        tarefas=tarefas,
        provas=preparar_provas_para_tela(provas),
        resultados=preparar_resultados_para_tela(resultados),
    )




def carregar_avaliacao(avaliacao_id: int):
    conn = conectar()
    try:
        pid = professor_atual_id()
        if pid:
            return conn.execute(
                "SELECT * FROM avaliacoes WHERE id = ? AND professor_id = ?",
                (int(avaliacao_id), int(pid)),
            ).fetchone()
        return conn.execute("SELECT * FROM avaliacoes WHERE id = ?", (int(avaliacao_id),)).fetchone()
    finally:
        conn.close()


def montar_dados_avaliacao(avaliacao):
    """Transforma o registro da avaliação em dict pronto para template/PDF."""
    dados = dict(avaliacao)
    questoes = carregar_json_seguro(dados.get("questoes_json"), [])
    gabarito = carregar_json_seguro(dados.get("gabarito_json"), {})
    dados["questoes"] = questoes if isinstance(questoes, list) else []
    dados["gabarito_professor"] = _ordenar_mapa_questoes(gabarito)
    dados["gabarito_formatado"] = formatar_gabarito(gabarito)
    return dados


def _texto_pdf(c, texto, x, y, largura, fonte="Helvetica", tamanho=10, entrelinha=13, margem_inferior=50):
    """Escreve texto quebrando linha e página. Retorna a nova posição y."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit

    _, altura_pagina = A4
    texto = str(texto or "").replace("\n", " ")
    linhas = simpleSplit(texto, fonte, tamanho, largura)

    c.setFont(fonte, tamanho)
    for linha in linhas:
        if y < margem_inferior:
            c.showPage()
            y = altura_pagina - 50
            c.setFont(fonte, tamanho)
        c.drawString(x, y, linha)
        y -= entrelinha
    return y


def gerar_pdf_prova(avaliacao, incluir_gabarito=False):
    """Gera PDF no modelo visual V1 enviado pelo usuário.

    O layout segue a referência atlas-teste-v1-impressao:
    - cabeçalho limpo com marca Atlas;
    - caixa V1 no canto superior direito;
    - cards de disciplina/assunto/turma/data;
    - prova com questões e alternativas;
    - gabarito com cards das respostas e resoluções.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    from reportlab.lib.utils import simpleSplit

    PDF_DIR = BASE_DIR / "static" / "pdfs"
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    dados = montar_dados_avaliacao(avaliacao)
    sufixo = "gabarito" if incluir_gabarito else "prova"
    caminho = PDF_DIR / f"avaliacao_{dados['id']}_{sufixo}_v1.pdf"

    c = canvas.Canvas(str(caminho), pagesize=A4)
    W, H = A4
    azul = colors.HexColor("#003c78")
    texto = colors.HexColor("#0c2340")
    muted = colors.HexColor("#5c6d86")
    border = colors.HexColor("#c5d4e5")
    fill = colors.HexColor("#f7fbff")

    margem = 1.7 * cm
    x0 = margem
    y = H - 1.6 * cm

    def draw_logo(x, y_top):
        c.setStrokeColor(texto)
        c.setLineWidth(1.2)
        r = 13
        c.circle(x + r, y_top - r, r)
        c.ellipse(x + 5, y_top - 24, x + 21, y_top - 2)
        c.ellipse(x + 2, y_top - 18, x + 24, y_top - 8)
        c.line(x + r, y_top - 26, x + r - 7, y_top - 39)
        c.line(x + r, y_top - 26, x + r + 9, y_top - 39)
        c.line(x + 2, y_top - 39, x + 28, y_top - 39)

    def draw_box(x, y_top, w, h, label, value, value_size=10):
        c.setStrokeColor(border)
        c.setFillColor(fill)
        c.rect(x, y_top - h, w, h, stroke=1, fill=1)
        c.setFillColor(muted)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(x + 7, y_top - 14, str(label).upper())
        c.setFillColor(texto)
        c.setFont("Helvetica-Bold", value_size)
        c.drawString(x + 7, y_top - 34, str(value or "-")[:38])

    def draw_wrapped(texto_str, x, y_atual, largura, font="Helvetica", size=9.5, leading=12, min_y=45):
        texto_str = str(texto_str or "").replace("\n", " ")
        linhas = simpleSplit(texto_str, font, size, largura)
        c.setFont(font, size)
        c.setFillColor(texto)
        for linha in linhas:
            if y_atual < min_y:
                c.showPage()
                draw_header()
                y_atual = H - 4.2 * cm
                c.setFont(font, size)
                c.setFillColor(texto)
            c.drawString(x, y_atual, linha)
            y_atual -= leading
        return y_atual

    def draw_header():
        nonlocal y
        y = H - 1.6 * cm
        draw_logo(x0, y)
        c.setFillColor(muted)
        c.setFont("Helvetica-Bold", 8)
        tipo = "GABARITO DO PROFESSOR" if incluir_gabarito else "PROVA"
        c.drawString(x0 + 3.1 * cm, y - 12, tipo)
        c.setFillColor(texto)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(x0 + 3.1 * cm, y - 32, str(dados["titulo"])[:45])

        c.setStrokeColor(colors.HexColor("#9fc2e6"))
        c.setFillColor(colors.HexColor("#eaf4ff"))
        c.rect(W - margem - 52, y - 34, 52, 34, stroke=1, fill=1)
        c.setFillColor(azul)
        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(W - margem - 26, y - 21, "V1")

        c.setStrokeColor(azul)
        c.setLineWidth(2)
        c.line(x0, y - 64, W - margem, y - 64)

        y = y - 82
        gap = 7
        box_w = (W - 2 * margem - 3 * gap) / 4
        draw_box(x0, y, box_w, 46, "Disciplina", dados.get("materias") or "-")
        draw_box(x0 + (box_w + gap), y, box_w, 46, "Assunto", dados.get("temas") or dados.get("materias") or "-")
        draw_box(x0 + 2 * (box_w + gap), y, box_w, 46, "Turma", "-")
        draw_box(x0 + 3 * (box_w + gap), y, box_w, 46, "Data / Tempo", datetime.now().strftime("%d/%m/%Y"))
        y -= 66

    draw_header()

    if incluir_gabarito:
        gabarito = dados["gabarito_professor"]
        q_w = (W - 2 * margem - 4 * 7) / 5
        x = x0
        y_top = y
        for idx, (questao, resposta) in enumerate(gabarito.items(), start=1):
            draw_box(x, y_top, q_w, 52, f"Questão {questao}", resposta, value_size=16)
            x += q_w + 7
            if idx % 5 == 0:
                x = x0
                y_top -= 58
        y = y_top - 26
        c.setFillColor(muted)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x0, y, "RESOLUÇÕES E OBSERVAÇÕES")
        y -= 17
        for q in dados["questoes"]:
            if y < 80:
                c.showPage()
                draw_header()
            numero = q.get("numero", "")
            enunciado = q.get("enunciado", "")
            explicacao = q.get("explicacao", "")
            correta = str(q.get("correta", dados["gabarito_professor"].get(str(numero), ""))).upper()
            c.setFont("Helvetica-Bold", 9.5)
            c.setFillColor(texto)
            y = draw_wrapped(f"{numero}. {enunciado}", x0, y, W - 2 * margem, font="Helvetica-Bold", size=9.5, leading=12)
            c.setFillColor(muted)
            y = draw_wrapped(f"Resposta: {correta} - {explicacao}", x0, y, W - 2 * margem, font="Helvetica", size=8.8, leading=11)
            c.setStrokeColor(colors.HexColor("#d7e2ef"))
            c.line(x0, y - 5, W - margem, y - 5)
            y -= 17
    else:
        # Campo para nome do aluno, como no modelo.
        c.setStrokeColor(border)
        c.setFillColor(fill)
        c.rect(x0, y - 30, W - 2 * margem, 30, stroke=1, fill=1)
        c.setFillColor(muted)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x0 + 8, y - 19, "ALUNO:")
        c.setStrokeColor(colors.HexColor("#7c8ca2"))
        c.line(x0 + 55, y - 20, W - margem - 18, y - 20)
        y -= 48

        for q in dados["questoes"]:
            if y < 115:
                c.showPage()
                draw_header()
            numero = q.get("numero", "")
            c.setFillColor(azul)
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x0, y, f"{numero}.")
            y = draw_wrapped(q.get("enunciado", ""), x0 + 28, y, W - 2 * margem - 28, font="Helvetica-Bold", size=10.5, leading=13)
            alternativas = q.get("alternativas", {}) if isinstance(q.get("alternativas"), dict) else {}
            for alt in ["A", "B", "C", "D", "E"]:
                y = draw_wrapped(f"{alt}) {alternativas.get(alt, '')}", x0 + 20, y, W - 2 * margem - 20, size=9.5, leading=12)
            y -= 9

    c.save()
    return caminho


@app.route("/criar-prova", methods=["POST"])
def criar_prova():
    aluno_id = int(request.form["aluno_id"])
    titulo = request.form.get("titulo", "Simulado Modelo ENEM")
    disciplina = request.form.get("disciplina", "Linguagens/Humanas")
    total_questoes = int(request.form.get("total_questoes", 45))
    gabarito_json = request.form.get("gabarito_json", "{}")

    try:
        gabarito = json.loads(gabarito_json)
    except json.JSONDecodeError:
        flash("Gabarito inválido.", "danger")
        return redirect(url_for("home"))

    conn = conectar()

    aluno = conn.execute(
        "SELECT * FROM alunos WHERE id = ? AND professor_id = ?", (aluno_id, int(professor_atual_id()))
    ).fetchone()

    cursor = conn.execute(
        """
        INSERT INTO provas (professor_id, titulo, disciplina, aluno_id, total_questoes, gabarito_json, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        (
            int(professor_atual_id()),
            titulo,
            disciplina,
            aluno_id,
            total_questoes,
            json.dumps(gabarito, ensure_ascii=False),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )

    prova_id = cursor.lastrowid
    folha_codigo = gerar_codigo_folha(prova_id, aluno_id)
    qr_arquivo = gerar_qr(aluno, prova_id, titulo, disciplina, folha_codigo=folha_codigo)

    conn.execute(
        "UPDATE provas SET qr_arquivo = ?, folha_codigo = ? WHERE id = ?",
        (qr_arquivo, folha_codigo, prova_id),
    )
    conn.commit()
    conn.close()

    flash("Prova criada com sucesso.", "success")
    return redirect(url_for("home"))


@app.route("/folha/<int:prova_id>")
def folha(prova_id):
    conn = conectar()

    prova = conn.execute(
        """
        SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
        FROM provas
        JOIN alunos ON alunos.id = provas.aluno_id
        WHERE provas.id = ? AND provas.professor_id = ?
    """,
        (prova_id, int(professor_atual_id())),
    ).fetchone()

    if prova and (not prova["folha_codigo"] or not prova["qr_arquivo"]):
        aluno = conn.execute("SELECT * FROM alunos WHERE id = ?", (int(prova["aluno_id"]),)).fetchone()
        folha_codigo = prova["folha_codigo"] or gerar_codigo_folha(prova_id, prova["aluno_id"])
        qr_arquivo = prova["qr_arquivo"] or gerar_qr(aluno, prova_id, prova["titulo"], prova["disciplina"], folha_codigo=folha_codigo)
        conn.execute(
            "UPDATE provas SET folha_codigo = ?, qr_arquivo = ? WHERE id = ?",
            (folha_codigo, qr_arquivo, int(prova_id)),
        )
        conn.commit()
        prova = conn.execute(
            """
            SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
            FROM provas
            JOIN alunos ON alunos.id = provas.aluno_id
            WHERE provas.id = ? AND provas.professor_id = ?
            """,
            (prova_id, int(professor_atual_id())),
        ).fetchone()

    conn.close()

    if not prova:
        flash("Prova não encontrada.", "danger")
        return redirect(url_for("home"))

    return render_template("folha.html", prova=prova)




@app.route("/gerar-prova-automatica", methods=["POST"])
def gerar_prova_automatica():
    """
    Novo fluxo assíncrono:
    - O clique do professor cria uma tarefa em milissegundos;
    - Um worker em segundo plano usa cache/IA/fallback;
    - A tela de status recebe atualização em tempo real por SSE.
    """
    titulo = request.form.get("titulo_auto", "Simulado Modelo ENEM")
    materias = request.form.get("materias_auto", "Conhecimentos gerais")
    temas = request.form.get("temas_auto", "")
    especificacoes = request.form.get("especificacoes_auto", "")
    # Modo interno fixo: o professor não escolhe mais o modo.
    # O sistema decide automaticamente entre cache, IA e fallback para manter rapidez e estabilidade.
    modo_geracao = "inteligente"

    try:
        total_questoes = int(request.form.get("total_questoes_auto", 10))
    except Exception:
        total_questoes = 10

    total_questoes = max(1, min(total_questoes, 90))

    dados = {
        "professor_id": int(professor_atual_id()),
        "titulo": titulo,
        "materias": materias,
        "temas": temas,
        "especificacoes": especificacoes,
        "total_questoes": total_questoes,
        "modo_geracao": modo_geracao,
        "permitir_ia": request.form.get("permitir_ia_auto") == "on",
    }

    tarefa_id = criar_tarefa_geracao(dados)
    iniciar_worker_fila()

    flash("Pedido recebido. A prova será gerada em segundo plano sem travar o sistema.", "info")
    return redirect(url_for("status_tarefa", tarefa_id=tarefa_id))


@app.route("/tarefa/<int:tarefa_id>")
def status_tarefa(tarefa_id):
    tarefa = carregar_tarefa(tarefa_id)
    if not tarefa or int(tarefa["professor_id"] or 0) != int(professor_atual_id()):
        flash("Tarefa não encontrada.", "danger")
        return redirect(url_for("home"))
    return render_template("tarefa_status.html", tarefa=tarefa)


@app.route("/tarefa/<int:tarefa_id>/eventos")
def eventos_tarefa(tarefa_id):
    """Server-Sent Events: envia o status da geração em tempo real para o navegador."""

    @stream_with_context
    def gerar_eventos():
        ultimo_payload = None
        for _ in range(0, 600):  # até 10 min; depois a página pode ser recarregada.
            tarefa = carregar_tarefa(tarefa_id)
            if not tarefa or int(tarefa["professor_id"] or 0) != int(professor_atual_id()):
                payload = {"status": "erro", "progresso": 100, "mensagem": "Tarefa não encontrada."}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                break

            resultado_id = tarefa["resultado_id"]
            payload = {
                "id": tarefa["id"],
                "status": tarefa["status"],
                "progresso": tarefa["progresso"] or 0,
                "mensagem": tarefa["mensagem"] or "",
                "erro": tarefa["erro"] or "",
                "resultado_id": resultado_id,
                "url_avaliacao": url_for("ver_avaliacao", avaliacao_id=resultado_id) if resultado_id else None,
                "url_folhas": url_for("selecionar_alunos_avaliacao", avaliacao_id=resultado_id) if resultado_id else None,
                "url_pdf": url_for("baixar_pdf_avaliacao", avaliacao_id=resultado_id) if resultado_id else None,
            }

            texto = json.dumps(payload, ensure_ascii=False)
            if texto != ultimo_payload:
                yield f"data: {texto}\n\n"
                ultimo_payload = texto

            if tarefa["status"] in ["concluido", "erro"]:
                break
            time.sleep(1)

    return Response(gerar_eventos(), mimetype="text/event-stream")


@app.route("/avaliacao/<int:avaliacao_id>")
def ver_avaliacao(avaliacao_id):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "danger")
        return redirect(url_for("home"))
    return render_template("prova_gerada.html", avaliacao=montar_dados_avaliacao(avaliacao))


@app.route("/avaliacao/<int:avaliacao_id>/pdf")
def baixar_pdf_avaliacao(avaliacao_id):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "danger")
        return redirect(url_for("home"))
    caminho = gerar_pdf_prova(avaliacao, incluir_gabarito=False)
    return send_file(caminho, as_attachment=True, download_name=f"prova_{avaliacao_id}.pdf")


@app.route("/avaliacao/<int:avaliacao_id>/gabarito")
def ver_gabarito_avaliacao(avaliacao_id):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "danger")
        return redirect(url_for("home"))
    return render_template("gabarito_professor.html", avaliacao=montar_dados_avaliacao(avaliacao))


@app.route("/avaliacao/<int:avaliacao_id>/gabarito-pdf")
def baixar_pdf_gabarito(avaliacao_id):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "danger")
        return redirect(url_for("home"))
    caminho = gerar_pdf_prova(avaliacao, incluir_gabarito=True)
    return send_file(caminho, as_attachment=True, download_name=f"gabarito_professor_{avaliacao_id}.pdf")


@app.route("/avaliacao/<int:avaliacao_id>/gerar-folhas", methods=["GET", "POST"])
def selecionar_alunos_avaliacao(avaliacao_id):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "danger")
        return redirect(url_for("home"))

    conn = conectar()

    if request.method == "POST":
        alunos_ids = []

        for valor in request.form.getlist("aluno_ids"):
            try:
                alunos_ids.append(int(valor))
            except Exception:
                pass

        turmas = [t for t in request.form.getlist("turmas") if t.strip()]
        if turmas:
            placeholders = ",".join("?" for _ in turmas)
            alunos_turma = conn.execute(
                f"SELECT id FROM alunos WHERE professor_id = ? AND turma IN ({placeholders})",
                [int(professor_atual_id()), *turmas],
            ).fetchall()
            alunos_ids.extend([int(a["id"]) for a in alunos_turma])

        alunos_ids = sorted(set(alunos_ids))

        if not alunos_ids:
            conn.close()
            flash("Selecione pelo menos um aluno ou uma turma.", "warning")
            return redirect(url_for("selecionar_alunos_avaliacao", avaliacao_id=avaliacao_id))

        dados_avaliacao = montar_dados_avaliacao(avaliacao)
        gabarito_json = json.dumps(dados_avaliacao["gabarito_professor"], ensure_ascii=False)
        folhas_geradas = []
        ja_existentes = []

        for aluno_id in alunos_ids:
            aluno = conn.execute("SELECT * FROM alunos WHERE id = ? AND professor_id = ?", (aluno_id, int(professor_atual_id()))).fetchone()
            if not aluno:
                continue

            existente = conn.execute(
                "SELECT * FROM provas WHERE avaliacao_id = ? AND aluno_id = ? AND professor_id = ? LIMIT 1",
                (avaliacao_id, aluno_id, int(professor_atual_id())),
            ).fetchone()

            if existente:
                ja_existentes.append(existente)
                folhas_geradas.append(existente)
                continue

            cursor = conn.execute(
                """
                INSERT INTO provas
                    (professor_id, titulo, disciplina, aluno_id, total_questoes, gabarito_json, qr_arquivo, criado_em, avaliacao_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(professor_atual_id()),
                    dados_avaliacao["titulo"],
                    dados_avaliacao["materias"],
                    aluno_id,
                    dados_avaliacao["total_questoes"],
                    gabarito_json,
                    None,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    avaliacao_id,
                ),
            )
            prova_id = cursor.lastrowid
            folha_codigo = gerar_codigo_folha(prova_id, aluno_id)
            qr_arquivo = gerar_qr(
                aluno,
                prova_id,
                dados_avaliacao["titulo"],
                dados_avaliacao["materias"],
                folha_codigo=folha_codigo,
            )
            conn.execute(
                "UPDATE provas SET qr_arquivo = ?, folha_codigo = ? WHERE id = ?",
                (qr_arquivo, folha_codigo, prova_id),
            )

            prova_linha = conn.execute(
                """
                SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
                FROM provas
                JOIN alunos ON alunos.id = provas.aluno_id
                WHERE provas.id = ?
                """,
                (prova_id,),
            ).fetchone()
            folhas_geradas.append(prova_linha)

        conn.commit()

        # Recarrega existentes com dados do aluno para exibir corretamente.
        if ja_existentes:
            ids = [int(p["id"]) for p in folhas_geradas if p]
            placeholders = ",".join("?" for _ in ids)
            folhas_geradas = conn.execute(
                f"""
                SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
                FROM provas
                JOIN alunos ON alunos.id = provas.aluno_id
                WHERE provas.professor_id = ? AND provas.id IN ({placeholders})
                ORDER BY alunos.nome
                """,
                [int(professor_atual_id()), *ids],
            ).fetchall()

        conn.close()
        flash(f"{len(folhas_geradas)} folha(s) de resposta pronta(s) para impressão.", "success")
        return render_template(
            "folhas_geradas.html",
            avaliacao=montar_dados_avaliacao(avaliacao),
            folhas=folhas_geradas,
        )

    alunos = conn.execute("SELECT * FROM alunos WHERE professor_id = ? ORDER BY turma, nome", (int(professor_atual_id()),)).fetchall()
    turmas = conn.execute("SELECT DISTINCT turma FROM alunos WHERE professor_id = ? ORDER BY turma", (int(professor_atual_id()),)).fetchall()
    conn.close()

    return render_template(
        "selecionar_alunos.html",
        avaliacao=montar_dados_avaliacao(avaliacao),
        alunos=alunos,
        turmas=turmas,
    )




@app.route("/scan")
def scan_tempo_real():
    conn = conectar()
    try:
        provas = conn.execute(
            """
            SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
            FROM provas
            JOIN alunos ON alunos.id = provas.aluno_id
            WHERE provas.professor_id = ?
            ORDER BY provas.id DESC
            """,
            (int(professor_atual_id()),),
        ).fetchall()

        sessoes = conn.execute(
            """
            SELECT * FROM sessoes_scan
            WHERE professor_id = ?
            ORDER BY id DESC
            LIMIT 8
            """,
            (int(professor_atual_id()),),
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        "scan.html",
        provas=preparar_provas_para_tela(provas),
        sessoes=sessoes,
    )


@app.route("/scan/iniciar", methods=["POST"])
def iniciar_sessao_scan_rota():
    dados = request.get_json(silent=True) or request.form

    try:
        prova_base_id = int(dados.get("prova_id") or dados.get("prova_base_id") or 0)
    except Exception:
        return jsonify({"ok": False, "mensagem": "Selecione uma prova base válida."}), 400

    modo = str(dados.get("modo") or "individual").strip().lower()
    if modo not in ["individual", "multipla"]:
        modo = "individual"

    conn = conectar()
    try:
        prova = conn.execute("SELECT id FROM provas WHERE id = ? AND professor_id = ?", (prova_base_id, int(professor_atual_id()))).fetchone()
        if not prova:
            return jsonify({"ok": False, "mensagem": "Prova base não encontrada."}), 404

        cursor = conn.execute(
            """
            INSERT INTO sessoes_scan (professor_id, prova_base_id, modo, status, criado_em)
            VALUES (?, ?, ?, 'aberta', ?)
            """,
            (int(professor_atual_id()), prova_base_id, modo, agora_str()),
        )
        sessao_id = cursor.lastrowid
        conn.commit()

        return jsonify({
            "ok": True,
            "sessao_id": sessao_id,
            "modo": modo,
            "mensagem": "Sessão de Scan iniciada.",
        })
    finally:
        conn.close()


@app.route("/scan/finalizar", methods=["POST"])
def finalizar_sessao_scan_rota():
    dados = request.get_json(silent=True) or request.form
    try:
        sessao_id = int(dados.get("sessao_id") or 0)
    except Exception:
        return jsonify({"ok": False, "mensagem": "Sessão inválida."}), 400

    conn = conectar()
    try:
        conn.execute(
            "UPDATE sessoes_scan SET status = 'finalizada', finalizada_em = ? WHERE id = ? AND professor_id = ?",
            (agora_str(), sessao_id, int(professor_atual_id())),
        )
        resumo = recalcular_sessao_scan(conn, sessao_id)
        conn.commit()
        return jsonify({"ok": True, "sessao_id": sessao_id, "resumo": resumo})
    finally:
        conn.close()


@app.route("/scan/processar-frame", methods=["POST"])
def processar_frame_scan_rota():
    """
    Recebe um frame da câmera e corrige usando o MESMO pipeline do Upload.

    Diferença de fluxo:
    - Frame sem QR no Scan não cria pendência automaticamente.
    - Só cria pendência quando o frontend enviar forcar_pendencia=True.
    - Nunca usa prova/aluno base para salvar resultado.
    """
    dados = request.get_json(silent=True) or {}

    try:
        sessao_id = int(dados.get("sessao_id") or 0)
        prova_base_id = int(dados.get("prova_id") or dados.get("prova_base_id") or 0)
    except Exception:
        return jsonify({"ok": False, "status": "erro", "mensagem": "Sessão ou prova inválida.", "auto_continuar": False}), 400

    imagem_data_url = dados.get("imagem")
    forcar_pendencia = bool(dados.get("forcar_pendencia"))
    inicio = time.perf_counter()

    try:
        caminho = salvar_frame_base64(imagem_data_url)
    except Exception as erro:
        return jsonify({"ok": False, "status": "erro", "mensagem": str(erro), "auto_continuar": False}), 400

    conn = conectar()
    try:
        sessao = conn.execute(
            "SELECT * FROM sessoes_scan WHERE id = ? AND professor_id = ?",
            (sessao_id, int(professor_atual_id())),
        ).fetchone()
        if not sessao or sessao["status"] != "aberta":
            return jsonify({"ok": False, "status": "erro", "mensagem": "Sessão de Scan não está aberta.", "auto_continuar": False}), 400

        # A prova base existe apenas para abrir a sessão. Ela NÃO é fallback de identificação.
        prova_base = buscar_prova_com_aluno(conn, prova_base_id)
        if not prova_base:
            return jsonify({"ok": False, "status": "erro", "mensagem": "Prova base não encontrada.", "auto_continuar": False}), 404

        pipeline = corrigir_folha_por_pipeline_unico(
            conn,
            caminho,
            origem="scan",
            criar_pendencia=forcar_pendencia,
            nome_arquivo="frame da câmera",
        )

        latencia_ms = round((time.perf_counter() - inicio) * 1000, 2)

        if not pipeline.get("ok"):
            if forcar_pendencia:
                conn.commit()

            resumo = recalcular_sessao_scan(conn, sessao_id)
            status = pipeline.get("status") or "erro"
            mensagem = pipeline.get("mensagem") or "Não foi possível corrigir."

            # Frame comum sem QR: devolve apenas orientação ao frontend; não cria lixo no banco.
            if status == "buscando_qr":
                mensagem = "QR não encontrado neste frame. Aproxime o QR da câmera, melhore o foco e tente novamente."

            return jsonify({
                "ok": False,
                "status": status,
                "mensagem": mensagem,
                "auto_continuar": False,
                "salvar_automaticamente": False,
                "pendencia_id": pipeline.get("pendencia_id"),
                "pendencia_url": pipeline.get("pendencia_url"),
                "latencia_ms": latencia_ms,
                "resumo": resumo,
            }), 200

        prova_corrigir = pipeline["prova"]
        aluno_id = int(pipeline["aluno_id"])
        prova_id = int(pipeline["prova_id"])
        resultado = pipeline["resultado"]
        nota = float(pipeline["nota"])
        status_leitura = pipeline.get("status") or "ok"
        mensagem = pipeline.get("mensagem") or "QR lido com sucesso."

        # Segurança extra: o QR deve apontar para o mesmo aluno ligado à folha/prova no banco.
        try:
            if int(prova_corrigir["aluno_id"]) != aluno_id:
                return jsonify({
                    "ok": False,
                    "status": "qr_invalido",
                    "mensagem": "QR lido, mas o aluno do QR não bate com a folha registrada no banco. Nada foi salvo.",
                    "auto_continuar": False,
                    "salvar_automaticamente": False,
                    "latencia_ms": latencia_ms,
                    "resumo": recalcular_sessao_scan(conn, sessao_id),
                }), 200
        except Exception:
            pass

        leitura_existente = conn.execute(
            """
            SELECT leituras_scan.*, resultados.nota_percentual
            FROM leituras_scan
            LEFT JOIN resultados ON resultados.id = leituras_scan.resultado_id
            WHERE leituras_scan.sessao_id = ? AND leituras_scan.aluno_id = ? AND leituras_scan.prova_id = ?
            ORDER BY leituras_scan.id DESC
            LIMIT 1
            """,
            (sessao_id, aluno_id, prova_id),
        ).fetchone()

        if leitura_existente:
            resumo = recalcular_sessao_scan(conn, sessao_id)
            conn.commit()
            return jsonify({
                "ok": True,
                "status": "duplicado",
                "mensagem": "Esta folha já foi lida nesta sessão. Mostre outra folha para continuar.",
                "auto_continuar": False,
                "aluno": prova_corrigir["aluno_nome"],
                "prova": prova_corrigir["titulo"],
                "nota_percentual": leitura_existente["nota_percentual"],
                "latencia_ms": latencia_ms,
                "resumo": resumo,
            })

        resultado.setdefault("processamento", {})["scan"] = {
            "sessao_id": sessao_id,
            "latencia_ms": latencia_ms,
            "status_scan": status_leitura,
            "mensagem_scan": mensagem,
            "pipeline": "upload_unificado",
        }

        cursor = conn.execute(
            """
            INSERT INTO resultados (professor_id, aluno_id, prova_id, nota_percentual, resultado_json, status_confianca, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(professor_atual_id()),
                aluno_id,
                prova_id,
                nota,
                json.dumps(resultado, ensure_ascii=False),
                status_leitura,
                agora_str(),
            ),
        )
        resultado_id = cursor.lastrowid

        conn.execute(
            """
            INSERT INTO leituras_scan
                (sessao_id, aluno_id, prova_id, resultado_id, nota_percentual, latencia_ms, status, mensagem, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sessao_id,
                aluno_id,
                prova_id,
                resultado_id,
                nota,
                latencia_ms,
                status_leitura,
                mensagem,
                agora_str(),
            ),
        )

        resumo = recalcular_sessao_scan(conn, sessao_id)
        conn.commit()

        return jsonify({
            "ok": True,
            "status": status_leitura,
            "mensagem": mensagem,
            "auto_continuar": False,
            "sessao_id": sessao_id,
            "resultado_id": resultado_id,
            "aluno": prova_corrigir["aluno_nome"],
            "turma": prova_corrigir["turma"],
            "prova": prova_corrigir["titulo"],
            "nota_percentual": nota,
            "acertos": resultado["resultado"].get("acertos", 0),
            "erros": resultado["resultado"].get("erros", 0),
            "anuladas": resultado["resultado"].get("anuladas_ou_em_branco", 0),
            "total_questoes": resultado["resultado"].get("total_questoes_oficial", 0),
            "latencia_ms": latencia_ms,
            "lento": latencia_ms >= 1500,
            "resumo": resumo,
        })
    except Exception as erro:
        return jsonify({
            "ok": False,
            "status": "erro",
            "mensagem": f"Erro ao processar o frame: {erro}",
            "auto_continuar": False,
            "latencia_ms": round((time.perf_counter() - inicio) * 1000, 2),
        }), 500
    finally:
        conn.close()


@app.route("/scan/sessao/<int:sessao_id>")
def resumo_sessao_scan_rota(sessao_id):
    dados = carregar_resumo_sessao_scan(sessao_id)
    if not dados:
        flash("Sessão de Scan não encontrada.", "warning")
        return redirect(url_for("scan_tempo_real"))
    return render_template("scan_resumo.html", sessao=dados["sessao"], leituras=dados["leituras"])



@app.route("/alunos/criar", methods=["POST"])
def criar_aluno_rota():
    nome = request.form.get("nome", "").strip()
    matricula = request.form.get("matricula", "").strip()
    turma = request.form.get("turma", "").strip()
    if not nome or not matricula or not turma:
        flash("Preencha nome, matrícula e turma.", "warning")
        return redirect(url_for("home"))
    conn = conectar()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO alunos (professor_id, nome, matricula, turma, criado_em) VALUES (?, ?, ?, ?, ?)",
            (int(professor_atual_id()), nome, matricula, turma, agora_str()),
        )
        conn.commit()
        flash("Aluno cadastrado com sucesso.", "success")
    except Exception as erro:
        flash(f"Não foi possível cadastrar o aluno: {erro}", "danger")
    finally:
        conn.close()
    return redirect(url_for("home"))


@app.route("/alunos/importar-csv", methods=["POST"])
def importar_alunos_csv():
    arquivo = request.files.get("arquivo_csv")
    if not arquivo or arquivo.filename == "":
        flash("Envie um arquivo CSV com nome,turma,matricula.", "warning")
        return redirect(url_for("home"))
    try:
        texto = arquivo.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        arquivo.stream.seek(0)
        texto = arquivo.read().decode("latin-1")

    amostra = texto[:1024]
    try:
        dialect = csv.Sniffer().sniff(amostra, delimiters=",;")
    except Exception:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(texto), dialect=dialect)

    inseridos = 0
    ignorados = 0
    conn = conectar()
    try:
        for row in reader:
            normal = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
            nome = normal.get("nome") or normal.get("aluno") or normal.get("nome completo")
            turma = normal.get("turma") or normal.get("sala") or normal.get("classe")
            matricula = normal.get("matricula") or normal.get("matrícula") or normal.get("ra") or normal.get("rm")
            if not nome or not turma or not matricula:
                ignorados += 1
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO alunos (professor_id, nome, matricula, turma, criado_em) VALUES (?, ?, ?, ?, ?)",
                (int(professor_atual_id()), nome, matricula, turma, agora_str()),
            )
            inseridos += 1 if cur.rowcount else 0
        conn.commit()
    finally:
        conn.close()
    flash(f"Importação concluída: {inseridos} aluno(s) inserido(s), {ignorados} linha(s) ignorada(s).", "success")
    return redirect(url_for("home"))


def carregar_resultado_detalhado(resultado_id: int):
    conn = conectar()
    try:
        return conn.execute(
            """
            SELECT resultados.*, alunos.nome AS aluno_nome, alunos.turma, alunos.matricula,
                   provas.titulo AS prova_titulo, provas.gabarito_json, provas.total_questoes, provas.avaliacao_id
            FROM resultados
            LEFT JOIN alunos ON alunos.id = resultados.aluno_id
            JOIN provas ON provas.id = resultados.prova_id
            WHERE resultados.id = ? AND resultados.professor_id = ?
            """,
            (int(resultado_id), int(professor_atual_id())),
        ).fetchone()
    finally:
        conn.close()


@app.route("/resultado/<int:resultado_id>")
def ver_resultado_detalhado(resultado_id):
    resultado = carregar_resultado_detalhado(resultado_id)
    if not resultado:
        flash("Resultado não encontrado.", "warning")
        return redirect(url_for("home"))
    dados = carregar_json_seguro(resultado["resultado_json"], {})
    return render_template("resultado_detalhe.html", resultado=resultado, dados=dados)


def calcular_relatorio_avaliacao(avaliacao_id: int):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        return None
    dados_av = montar_dados_avaliacao(avaliacao)
    questoes = dados_av["questoes"]
    metadados = {str(q.get("numero")): q for q in questoes}

    conn = conectar()
    try:
        provas = conn.execute(
            """
            SELECT provas.*, alunos.nome AS aluno_nome, alunos.turma, alunos.matricula
            FROM provas
            JOIN alunos ON alunos.id = provas.aluno_id
            WHERE provas.avaliacao_id = ? AND provas.professor_id = ?
            ORDER BY alunos.turma, alunos.nome
            """,
            (int(avaliacao_id), int(professor_atual_id())),
        ).fetchall()

        resultados = conn.execute(
            """
            SELECT resultados.*, provas.aluno_id, alunos.nome AS aluno_nome, alunos.turma
            FROM resultados
            JOIN provas ON provas.id = resultados.prova_id
            LEFT JOIN alunos ON alunos.id = provas.aluno_id
            WHERE provas.avaliacao_id = ? AND resultados.professor_id = ?
            ORDER BY resultados.criado_em DESC, resultados.id DESC
            """,
            (int(avaliacao_id), int(professor_atual_id())),
        ).fetchall()
    finally:
        conn.close()

    # Mantém só o resultado mais recente por prova/aluno.
    vistos = set()
    unicos = []
    for r in resultados:
        chave = int(r["prova_id"])
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append(r)

    notas = [float(r["nota_percentual"] or 0) for r in unicos]
    media = round(sum(notas) / len(notas), 2) if notas else 0
    maior = round(max(notas), 2) if notas else 0
    menor = round(min(notas), 2) if notas else 0
    desvio = round(statistics.pstdev(notas), 2) if len(notas) > 1 else 0

    por_questao = {}
    por_conteudo = {}
    alunos_resultados = []

    for r in unicos:
        dados = carregar_json_seguro(r["resultado_json"], {})
        res = dados.get("resultado", {}) if isinstance(dados, dict) else {}
        alunos_resultados.append({"linha": r, "dados": dados, "resumo": res})
        for detalhe in res.get("detalhes", []) or []:
            qn = str(detalhe.get("questao"))
            item = por_questao.setdefault(qn, {"questao": qn, "acertos": 0, "erros": 0, "anuladas": 0, "total": 0})
            item["total"] += 1
            status = detalhe.get("status")
            if status == "correta":
                item["acertos"] += 1
            elif status == "errada":
                item["erros"] += 1
            else:
                item["anuladas"] += 1

            meta = metadados.get(qn, {})
            conteudo = str(meta.get("tema") or meta.get("area") or "Sem conteúdo informado").strip()
            cont = por_conteudo.setdefault(conteudo, {"conteudo": conteudo, "acertos": 0, "total": 0})
            cont["total"] += 1
            if status == "correta":
                cont["acertos"] += 1

    for item in por_questao.values():
        item["percentual_acerto"] = round((item["acertos"] / item["total"]) * 100, 2) if item["total"] else 0
        item["percentual_erro"] = round(((item["erros"] + item["anuladas"]) / item["total"]) * 100, 2) if item["total"] else 0
        meta = metadados.get(str(item["questao"]), {})
        item["conteudo"] = meta.get("tema") or meta.get("area") or "-"
        item["habilidade"] = meta.get("habilidade") or "-"

    for item in por_conteudo.values():
        item["percentual_acerto"] = round((item["acertos"] / item["total"]) * 100, 2) if item["total"] else 0

    return {
        "avaliacao": dados_av,
        "total_folhas": len(provas),
        "corrigidas": len(unicos),
        "media": media,
        "maior": maior,
        "menor": menor,
        "desvio": desvio,
        "por_questao": sorted(por_questao.values(), key=lambda x: int(x["questao"])),
        "questoes_criticas": sorted(por_questao.values(), key=lambda x: x["percentual_erro"], reverse=True)[:5],
        "por_conteudo": sorted(por_conteudo.values(), key=lambda x: x["percentual_acerto"]),
        "alunos_resultados": alunos_resultados,
    }


@app.route("/avaliacao/<int:avaliacao_id>/relatorio")
def relatorio_avaliacao(avaliacao_id):
    relatorio = calcular_relatorio_avaliacao(avaliacao_id)
    if not relatorio:
        flash("Avaliação não encontrada.", "warning")
        return redirect(url_for("home"))
    return render_template("relatorio_avaliacao.html", relatorio=relatorio)


@app.route("/avaliacao/<int:avaliacao_id>/editar", methods=["GET", "POST"])
def editar_avaliacao(avaliacao_id):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "warning")
        return redirect(url_for("home"))

    dados = montar_dados_avaliacao(avaliacao)
    if request.method == "POST":
        questoes = []
        gabarito = {}
        total = int(dados["total_questoes"])
        for i in range(1, total + 1):
            alts = {alt: request.form.get(f"q{i}_{alt}", "").strip() for alt in ["A", "B", "C", "D", "E"]}
            correta = request.form.get(f"q{i}_correta", "A").strip().upper()[:1]
            if correta not in ["A", "B", "C", "D", "E"]:
                correta = "A"
            questoes.append({
                "numero": i,
                "area": request.form.get(f"q{i}_area", "").strip(),
                "tema": request.form.get(f"q{i}_tema", "").strip(),
                "habilidade": request.form.get(f"q{i}_habilidade", "").strip(),
                "contexto": request.form.get(f"q{i}_contexto", "").strip(),
                "enunciado": request.form.get(f"q{i}_enunciado", "").strip(),
                "alternativas": alts,
                "correta": correta,
                "explicacao": request.form.get(f"q{i}_explicacao", "").strip(),
            })
            gabarito[str(i)] = correta
        conn = conectar()
        try:
            conn.execute(
                """
                UPDATE avaliacoes
                SET questoes_json = ?, gabarito_json = ?, status_revisao = 'revisada', atualizado_em = ?
                WHERE id = ? AND professor_id = ?
                """,
                (json.dumps(questoes, ensure_ascii=False), json.dumps(gabarito, ensure_ascii=False), agora_str(), int(avaliacao_id), int(professor_atual_id())),
            )
            conn.commit()
        finally:
            conn.close()
        flash("Avaliação revisada e gabarito atualizado.", "success")
        return redirect(url_for("ver_avaliacao", avaliacao_id=avaliacao_id))

    return render_template("editar_avaliacao.html", avaliacao=dados)


@app.route("/corrigir", methods=["POST"])
def corrigir():
    """
    Corrige uma ou várias folhas.

    Importante: a prova correta vem do QR Code da própria folha.
    O QR Code é obrigatório para salvar automaticamente.
    Se o QR falhar, a folha não é salva em aluno/prova base.
    """
    prova_base_id = int(request.form["prova_id"])
    arquivos = request.files.getlist("folhas")

    if not arquivos:
        flash("Envie pelo menos uma folha.", "danger")
        return redirect(url_for("home"))

    conn = conectar()
    prova_base = conn.execute(
        "SELECT * FROM provas WHERE id = ? AND professor_id = ?", (prova_base_id, int(professor_atual_id()))
    ).fetchone()

    if not prova_base:
        conn.close()
        flash("Prova base não encontrada.", "danger")
        return redirect(url_for("home"))

    total_corrigidas = 0
    avisos = []

    for arquivo in arquivos:
        if arquivo.filename == "":
            continue

        try:
            imagens_para_corrigir = preparar_arquivos_para_correcao(arquivo)
        except Exception as erro:
            avisos.append(f"Não consegui preparar {arquivo.filename}: {erro}")
            continue

        for caminho in imagens_para_corrigir:
            pipeline = corrigir_folha_por_pipeline_unico(
                conn,
                caminho,
                origem="upload",
                criar_pendencia=True,
                nome_arquivo=arquivo.filename,
            )

            if not pipeline.get("ok"):
                status = pipeline.get("status") or "erro"
                if status == "qr_nao_lido":
                    avisos.append(
                        f"Não consegui ler o QR de {arquivo.filename}. A folha foi enviada para Identificação Manual #{pipeline.get('pendencia_id')} e NÃO foi salva em aluno errado."
                    )
                elif status == "qr_invalido":
                    avisos.append(
                        f"QR de {arquivo.filename} apontou para uma prova inexistente ou de outro professor. A folha NÃO foi salva."
                    )
                else:
                    avisos.append(pipeline.get("mensagem") or f"Não foi possível corrigir {arquivo.filename}.")
                continue

            resultado = pipeline["resultado"]
            aluno_id = int(pipeline["aluno_id"])
            prova_id = int(pipeline["prova_id"])
            nota = float(pipeline["nota"])

            conn.execute(
                """
                INSERT INTO resultados (professor_id, aluno_id, prova_id, nota_percentual, resultado_json, status_confianca, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    int(professor_atual_id()),
                    aluno_id,
                    prova_id,
                    nota,
                    json.dumps(resultado, ensure_ascii=False),
                    pipeline.get("status") or "confiavel",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            total_corrigidas += 1

    conn.commit()
    conn.close()

    if total_corrigidas:
        flash(f"Correção finalizada. {total_corrigidas} folha(s) corrigida(s).", "success")
    else:
        flash("Nenhuma folha válida foi enviada.", "warning")

    for aviso in avisos[:5]:
        flash(aviso, "warning")

    if len(avisos) > 5:
        flash(f"Mais {len(avisos) - 5} aviso(s) ocultado(s).", "warning")

    return redirect(url_for("home"))


def salvar_pendencia_identificacao(conn, caminho_arquivo, origem="upload", mensagem="QR Code não lido."):
    """Salva uma folha sem QR para identificação manual posterior."""
    cursor = conn.execute(
        """
        INSERT INTO identificacoes_pendentes
            (professor_id, caminho_arquivo, origem, mensagem, resolvido, criado_em)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (
            int(professor_atual_id()),
            str(caminho_arquivo),
            origem,
            mensagem,
            agora_str(),
        ),
    )
    return int(cursor.lastrowid)


@app.route("/identificacoes-pendentes")
def identificacoes_pendentes():
    conn = conectar()
    try:
        pendentes = conn.execute(
            """
            SELECT * FROM identificacoes_pendentes
            WHERE professor_id = ? AND resolvido = 0
            ORDER BY id DESC
            """,
            (int(professor_atual_id()),),
        ).fetchall()
        resolvidas = conn.execute(
            """
            SELECT * FROM identificacoes_pendentes
            WHERE professor_id = ? AND resolvido = 1
            ORDER BY id DESC
            LIMIT 20
            """,
            (int(professor_atual_id()),),
        ).fetchall()
    finally:
        conn.close()
    return render_template("identificacoes_pendentes.html", pendentes=pendentes, resolvidas=resolvidas)


@app.route("/identificacao/<int:pendencia_id>/arquivo")
def arquivo_identificacao_pendente(pendencia_id):
    conn = conectar()
    try:
        pendencia = conn.execute(
            "SELECT * FROM identificacoes_pendentes WHERE id = ? AND professor_id = ?",
            (int(pendencia_id), int(professor_atual_id())),
        ).fetchone()
    finally:
        conn.close()

    if not pendencia:
        flash("Pendência não encontrada.", "warning")
        return redirect(url_for("identificacoes_pendentes"))

    caminho = Path(pendencia["caminho_arquivo"])
    if not caminho.exists():
        flash("Arquivo temporário da folha não existe mais. Envie a folha novamente.", "warning")
        return redirect(url_for("identificacoes_pendentes"))

    return send_file(caminho)


@app.route("/identificacao/<int:pendencia_id>/resolver", methods=["GET", "POST"])
def resolver_identificacao_pendente(pendencia_id):
    conn = conectar()
    try:
        pendencia = conn.execute(
            "SELECT * FROM identificacoes_pendentes WHERE id = ? AND professor_id = ?",
            (int(pendencia_id), int(professor_atual_id())),
        ).fetchone()

        if not pendencia:
            flash("Pendência não encontrada.", "warning")
            return redirect(url_for("identificacoes_pendentes"))

        provas = conn.execute(
            """
            SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
            FROM provas
            JOIN alunos ON alunos.id = provas.aluno_id
            WHERE provas.professor_id = ?
            ORDER BY provas.id DESC
            """,
            (int(professor_atual_id()),),
        ).fetchall()

        if request.method == "POST":
            prova_id = int(request.form.get("prova_id") or 0)
            prova = conn.execute(
                """
                SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
                FROM provas
                JOIN alunos ON alunos.id = provas.aluno_id
                WHERE provas.id = ? AND provas.professor_id = ?
                """,
                (prova_id, int(professor_atual_id())),
            ).fetchone()

            if not prova:
                flash("Folha/prova selecionada não encontrada.", "danger")
                return redirect(url_for("resolver_identificacao_pendente", pendencia_id=pendencia_id))

            caminho = Path(pendencia["caminho_arquivo"])
            if not caminho.exists():
                flash("Arquivo temporário da folha não existe mais. Envie a folha novamente.", "warning")
                return redirect(url_for("identificacoes_pendentes"))

            gabarito_oficial = json.loads(prova["gabarito_json"])
            resultado = corrigir_imagem_web(
                caminho_gabarito_aluno=str(caminho),
                gabarito_oficial=gabarito_oficial,
                usar_ia="auto",  # rollback seletivo: depois da identificação manual, usa o comportamento antigo tolerante quando o OMR falhar
            )
            resultado["dados_qr"] = {
                "modo": "identificacao_manual",
                "aluno_id": int(prova["aluno_id"]),
                "prova_id": int(prova["id"]),
                "nome": prova["aluno_nome"],
                "matricula": prova["matricula"],
                "turma": prova["turma"],
            }
            resultado.setdefault("processamento", {})["identificacao_manual"] = {
                "pendencia_id": int(pendencia_id),
                "confirmado_em": agora_str(),
            }

            nota = float(resultado["resultado"]["nota_percentual"])
            cursor = conn.execute(
                """
                INSERT INTO resultados
                    (professor_id, aluno_id, prova_id, nota_percentual, resultado_json, status_confianca, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(professor_atual_id()),
                    int(prova["aluno_id"]),
                    int(prova["id"]),
                    nota,
                    json.dumps(resultado, ensure_ascii=False),
                    "revisao_manual_qr",
                    agora_str(),
                ),
            )
            resultado_id = int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE identificacoes_pendentes
                SET resolvido = 1, resultado_id = ?, resolvido_em = ?
                WHERE id = ? AND professor_id = ?
                """,
                (resultado_id, agora_str(), int(pendencia_id), int(professor_atual_id())),
            )
            conn.commit()
            flash("Folha identificada manualmente e corrigida.", "success")
            return redirect(url_for("ver_resultado_detalhado", resultado_id=resultado_id))

        return render_template("resolver_identificacao.html", pendencia=pendencia, provas=provas)
    finally:
        conn.close()


if __name__ == "__main__":
    iniciar_worker_fila()
    porta = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=porta, debug=False, use_reloader=False)
