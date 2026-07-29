import base64
import csv
import hashlib
import io
import json
import os
import statistics
import threading
import random
import re
import difflib
import time
import uuid
from datetime import datetime
from pathlib import Path

import qrcode
from flask import Flask, request, redirect, url_for, flash, render_template, send_file, Response, stream_with_context, jsonify, session, g
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from bd import iniciar_banco, conectar
from corretor import corrigir_imagem_web, ler_qrcode, ler_imagem, localizar_marcadores_canto
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


def gerar_qr(aluno, prova_id, titulo, disciplina, tipo_prova="A"):
    dados = {
        "aluno_id": aluno["id"],
        "nome": aluno["nome"],
        "matricula": aluno["matricula"],
        "turma": aluno["turma"],
        "prova_id": prova_id,
        "titulo": titulo,
        "disciplina": disciplina,
        "tipo_prova": str(tipo_prova or "A").upper(),
    }

    nome = f"prova_{prova_id}_aluno_{aluno['id']}.png"
    caminho = QR_DIR / nome

    img = qrcode.make(json.dumps(dados, ensure_ascii=False))
    img.save(caminho)

    return nome



ALTERNATIVAS_ATLAS = ["A", "B", "C", "D", "E"]


def gerar_mapa_alternativas(total_questoes, tipo_prova="A", avaliacao_id=None, aluno_id=None):
    """Gera mapa impresso -> original para cada questão da folha.

    Tipo A é identidade. Tipo B embaralha de forma determinística por aluno,
    evitando que cada novo deploy mude o mapa salvo.
    """
    total = max(1, int(total_questoes or 1))
    tipo = str(tipo_prova or "A").strip().upper()[:1] or "A"
    identidade = {alt: alt for alt in ALTERNATIVAS_ATLAS}
    if tipo == "A":
        return {str(i): dict(identidade) for i in range(1, total + 1)}

    seed_txt = f"atlas|{avaliacao_id or 0}|{aluno_id or 0}|{tipo}|{total}"
    seed = int(hashlib.sha256(seed_txt.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    mapa = {}
    for i in range(1, total + 1):
        originais = list(ALTERNATIVAS_ATLAS)
        rng.shuffle(originais)
        # Evita uma questão ficar idêntica no Tipo B quando possível.
        if originais == ALTERNATIVAS_ATLAS:
            originais = originais[1:] + originais[:1]
        mapa[str(i)] = dict(zip(ALTERNATIVAS_ATLAS, originais))
    return mapa


def alternativa_correta_para_tipo(correta_original, mapa_questao):
    """Retorna a letra correta vista pelo aluno em uma folha Tipo A/B."""
    correta_original = str(correta_original or "").upper()[:1]
    if correta_original not in ALTERNATIVAS_ATLAS or not isinstance(mapa_questao, dict):
        return "NULA"
    for letra_impressa, letra_original in mapa_questao.items():
        if str(letra_original).upper()[:1] == correta_original:
            return letra_impressa
    return correta_original


def carregar_mapa_alternativas_linha(prova):
    if not prova:
        return {}
    try:
        return carregar_json_seguro(prova["mapa_alternativas_json"], {})
    except Exception:
        return {}

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
            # 2.5x melhora a leitura do QR e das bolhas sem deixar o arquivo absurdo.
            matriz = fitz.Matrix(2.5, 2.5)
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

    if len(dados) > 12_000_000:
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



def _faltosos_da_sessao(conn, sessao_id: int):
    sessao = conn.execute("SELECT * FROM sessoes_scan WHERE id = ?", (int(sessao_id),)).fetchone()
    if not sessao:
        return []
    prova_base = conn.execute("SELECT * FROM provas WHERE id = ?", (int(sessao["prova_base_id"] or 0),)).fetchone()
    if not prova_base or not prova_base["avaliacao_id"]:
        return []
    lidos = conn.execute(
        "SELECT DISTINCT aluno_id FROM leituras_scan WHERE sessao_id = ? AND status = 'ok' AND aluno_id IS NOT NULL",
        (int(sessao_id),),
    ).fetchall()
    ids_lidos = {int(x["aluno_id"]) for x in lidos}
    folhas = conn.execute(
        """
        SELECT provas.*, alunos.nome AS aluno_nome, alunos.turma, alunos.matricula
        FROM provas
        JOIN alunos ON alunos.id = provas.aluno_id
        WHERE provas.avaliacao_id = ? AND provas.professor_id = ?
        ORDER BY alunos.turma, alunos.nome
        """,
        (int(prova_base["avaliacao_id"]), int(sessao["professor_id"])),
    ).fetchall()
    return [dict(f) for f in folhas if int(f["aluno_id"]) not in ids_lidos]

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
        faltosos = _faltosos_da_sessao(conn, sessao_id)
        return {"sessao": sessao, "leituras": leituras, "faltosos": faltosos}
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
        WHERE professor_id = ? AND COALESCE(arquivado,0)=0
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
        WHERE provas.professor_id = ? AND COALESCE(provas.arquivado,0)=0
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
        WHERE resultados.professor_id = ? AND COALESCE(resultados.arquivado,0)=0
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
    """Gera PDF pronto para impressão da prova ou do gabarito do professor."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas

    PDF_DIR = BASE_DIR / "static" / "pdfs"
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    dados = montar_dados_avaliacao(avaliacao)
    sufixo = "gabarito" if incluir_gabarito else "prova"
    caminho = PDF_DIR / f"avaliacao_{dados['id']}_{sufixo}.pdf"

    c = canvas.Canvas(str(caminho), pagesize=A4)
    largura_pagina, altura_pagina = A4
    margem = 1.7 * cm
    largura_texto = largura_pagina - (2 * margem)
    y = altura_pagina - margem

    c.setTitle(f"{dados['titulo']} - {sufixo}")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margem, y, str(dados["titulo"])[:90])
    y -= 18
    c.setFont("Helvetica", 10)
    c.drawString(margem, y, f"Matérias: {dados['materias']}")
    y -= 14
    c.drawString(margem, y, f"Temas: {dados.get('temas') or '-'}")
    y -= 20

    if incluir_gabarito:
        c.setFont("Helvetica-Bold", 14)
        c.drawString(margem, y, "GABARITO OFICIAL DO PROFESSOR")
        y -= 22

        c.setFont("Helvetica", 11)
        for questao, resposta in dados["gabarito_professor"].items():
            if y < 55:
                c.showPage()
                y = altura_pagina - margem
                c.setFont("Helvetica", 11)
            c.drawString(margem, y, f"Questão {str(questao).zfill(2)}: {resposta}")
            y -= 15

        y -= 10
        c.setFont("Helvetica-Bold", 12)
        if y < 80:
            c.showPage()
            y = altura_pagina - margem
        c.drawString(margem, y, "Explicações resumidas")
        y -= 18
        for q in dados["questoes"]:
            y = _texto_pdf(
                c,
                f"{str(q.get('numero')).zfill(2)}. {q.get('explicacao', '')}",
                margem,
                y,
                largura_texto,
                tamanho=9,
                entrelinha=12,
            )
            y -= 5
    else:
        for q in dados["questoes"]:
            if y < 140:
                c.showPage()
                y = altura_pagina - margem

            c.setFont("Helvetica-Bold", 11)
            c.drawString(margem, y, f"Questão {str(q.get('numero')).zfill(2)} - {q.get('area', '')}")
            y -= 14
            y = _texto_pdf(c, q.get("contexto", ""), margem, y, largura_texto, tamanho=9, entrelinha=12)
            y -= 3
            y = _texto_pdf(c, q.get("enunciado", ""), margem, y, largura_texto, fonte="Helvetica-Bold", tamanho=10, entrelinha=13)
            y -= 4

            alternativas = q.get("alternativas", {}) if isinstance(q.get("alternativas"), dict) else {}
            for alt in ["A", "B", "C", "D", "E"]:
                y = _texto_pdf(
                    c,
                    f"{alt}) {alternativas.get(alt, '')}",
                    margem + 10,
                    y,
                    largura_texto - 10,
                    tamanho=9,
                    entrelinha=12,
                )
            y -= 10

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
        INSERT INTO provas (professor_id, titulo, disciplina, aluno_id, total_questoes, gabarito_json, tipo_prova, mapa_alternativas_json, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            int(professor_atual_id()),
            titulo,
            disciplina,
            aluno_id,
            total_questoes,
            json.dumps(gabarito, ensure_ascii=False),
            "A",
            json.dumps(gerar_mapa_alternativas(total_questoes, "A", None, aluno_id), ensure_ascii=False),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )

    prova_id = cursor.lastrowid
    qr_arquivo = gerar_qr(aluno, prova_id, titulo, disciplina, "A")

    conn.execute(
        "UPDATE provas SET qr_arquivo = ? WHERE id = ?", (qr_arquivo, prova_id)
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

    conn.close()

    if not prova:
        flash("Prova não encontrada.", "danger")
        return redirect(url_for("home"))

    return render_template("folha.html", prova=prova)





def montar_questoes_para_tipo(avaliacao, prova):
    """Monta questões com alternativas na ordem específica da folha do aluno."""
    dados = montar_dados_avaliacao(avaliacao)
    mapa = carregar_mapa_alternativas_linha(prova)
    questoes_tipo = []
    for q in dados.get("questoes", []):
        numero = str(q.get("numero"))
        alternativas_originais = q.get("alternativas", {}) if isinstance(q.get("alternativas", {}), dict) else {}
        mapa_q = mapa.get(numero, {alt: alt for alt in ALTERNATIVAS_ATLAS}) if isinstance(mapa, dict) else {alt: alt for alt in ALTERNATIVAS_ATLAS}
        alternativas_impressas = {}
        for alt_impressa in ALTERNATIVAS_ATLAS:
            alt_original = str(mapa_q.get(alt_impressa, alt_impressa)).upper()[:1]
            alternativas_impressas[alt_impressa] = alternativas_originais.get(alt_original, "")
        novo = dict(q)
        novo["alternativas"] = alternativas_impressas
        novo["mapa_alternativas"] = mapa_q
        novo["correta_impressa"] = alternativa_correta_para_tipo(q.get("correta"), mapa_q)
        questoes_tipo.append(novo)
    dados["questoes"] = questoes_tipo
    return dados


@app.route("/prova/<int:prova_id>/tipo")
def prova_tipo_aluno(prova_id):
    conn = conectar()
    try:
        prova = conn.execute(
            """
            SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
            FROM provas
            JOIN alunos ON alunos.id = provas.aluno_id
            WHERE provas.id = ? AND provas.professor_id = ?
            """,
            (prova_id, int(professor_atual_id())),
        ).fetchone()
        if not prova or not prova["avaliacao_id"]:
            flash("Prova personalizada não encontrada.", "danger")
            return redirect(url_for("home"))
        avaliacao = conn.execute(
            "SELECT * FROM avaliacoes WHERE id = ? AND professor_id = ?",
            (int(prova["avaliacao_id"]), int(professor_atual_id())),
        ).fetchone()
    finally:
        conn.close()
    if not avaliacao:
        flash("Avaliação vinculada não encontrada.", "danger")
        return redirect(url_for("home"))
    return render_template(
        "prova_tipo_aluno.html",
        prova=prova,
        avaliacao=montar_questoes_para_tipo(avaliacao, prova),
    )


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
                # Garante compatibilidade para folhas antigas criadas antes do Tipo A/B.
                tipo_existente = existente["tipo_prova"] if "tipo_prova" in existente.keys() and existente["tipo_prova"] else "A"
                mapa_existente = existente["mapa_alternativas_json"] if "mapa_alternativas_json" in existente.keys() and existente["mapa_alternativas_json"] else json.dumps(gerar_mapa_alternativas(dados_avaliacao["total_questoes"], tipo_existente, avaliacao_id, aluno_id), ensure_ascii=False)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO folhas_resposta
                        (professor_id, avaliacao_id, prova_id, aluno_id, tipo_prova, mapa_alternativas_json, criado_em)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (int(professor_atual_id()), avaliacao_id, int(existente["id"]), aluno_id, tipo_existente, mapa_existente, agora_str()),
                )
                ja_existentes.append(existente)
                folhas_geradas.append(existente)
                continue

            cursor = conn.execute(
                """
                INSERT INTO provas
                    (professor_id, titulo, disciplina, aluno_id, total_questoes, gabarito_json, qr_arquivo, tipo_prova, mapa_alternativas_json, criado_em, avaliacao_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(professor_atual_id()),
                    dados_avaliacao["titulo"],
                    dados_avaliacao["materias"],
                    aluno_id,
                    dados_avaliacao["total_questoes"],
                    gabarito_json,
                    None,
                    "A" if (len(folhas_geradas) % 2 == 0) else "B",
                    "{}",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    avaliacao_id,
                ),
            )
            prova_id = cursor.lastrowid
            tipo_prova = "A" if (len(folhas_geradas) % 2 == 0) else "B"
            mapa_alternativas = gerar_mapa_alternativas(dados_avaliacao["total_questoes"], tipo_prova, avaliacao_id, aluno_id)
            qr_arquivo = gerar_qr(aluno, prova_id, dados_avaliacao["titulo"], dados_avaliacao["materias"], tipo_prova)
            conn.execute(
                "UPDATE provas SET qr_arquivo = ?, tipo_prova = ?, mapa_alternativas_json = ? WHERE id = ?",
                (qr_arquivo, tipo_prova, json.dumps(mapa_alternativas, ensure_ascii=False), prova_id),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO folhas_resposta
                    (professor_id, avaliacao_id, prova_id, aluno_id, tipo_prova, mapa_alternativas_json, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (int(professor_atual_id()), avaliacao_id, prova_id, aluno_id, tipo_prova, json.dumps(mapa_alternativas, ensure_ascii=False), agora_str()),
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
        faltosos = _faltosos_da_sessao(conn, sessao_id)
        conn.commit()
        return jsonify({"ok": True, "sessao_id": sessao_id, "resumo": resumo, "faltosos": faltosos, "mensagem": f"Scan concluído. {resumo.get('total_sucesso',0)} folha(s) corrigida(s). Faltosos: {len(faltosos)}."})
    finally:
        conn.close()


@app.route("/scan/processar-frame", methods=["POST"])
def processar_frame_scan_rota():
    """Recebe um frame da câmera, corrige por OpenCV e devolve o resultado em JSON."""
    dados = request.get_json(silent=True) or {}

    try:
        sessao_id = int(dados.get("sessao_id") or 0)
        prova_base_id = int(dados.get("prova_id") or dados.get("prova_base_id") or 0)
    except Exception:
        return jsonify({"ok": False, "status": "erro", "mensagem": "Sessão ou prova inválida."}), 400

    imagem_data_url = dados.get("imagem")
    inicio = time.perf_counter()

    try:
        caminho = salvar_frame_base64(imagem_data_url)
    except Exception as erro:
        return jsonify({"ok": False, "status": "erro", "mensagem": str(erro)}), 400

    conn = conectar()
    try:
        sessao = conn.execute("SELECT * FROM sessoes_scan WHERE id = ? AND professor_id = ?", (sessao_id, int(professor_atual_id()))).fetchone()
        if not sessao or sessao["status"] != "aberta":
            return jsonify({"ok": False, "status": "erro", "mensagem": "Sessão de Scan não está aberta."}), 400

        prova_base = buscar_prova_com_aluno(conn, prova_base_id)
        if not prova_base:
            return jsonify({"ok": False, "status": "erro", "mensagem": "Prova base não encontrada."}), 404

        dados_qr = ler_qrcode(str(caminho))
        prova_corrigir = prova_base
        mensagem_qr = "QR não lido; usei a prova base como fallback."

        if dados_qr and dados_qr.get("prova_id"):
            prova_qr = buscar_prova_com_aluno(conn, int(dados_qr["prova_id"]))
            if prova_qr:
                prova_corrigir = prova_qr
                mensagem_qr = "QR lido com sucesso."
            else:
                mensagem_qr = "QR lido, mas a prova apontada não existe; usei a prova base."

        aluno_id = (
            int(dados_qr["aluno_id"])
            if dados_qr and dados_qr.get("aluno_id")
            else int(prova_corrigir["aluno_id"])
        )
        prova_id = int(prova_corrigir["id"])

        # Evita salvar/mostrar a mesma folha repetidamente durante uma sessão em lote.
        # Antes o backend retornava status "duplicado" a cada frame e o frontend
        # adicionava vários cards iguais na lista. Agora duplicata confirmada é
        # apenas ignorada, sem entrar no painel de "Últimas leituras".
        leitura_confirmada = conn.execute(
            """
            SELECT leituras_scan.*, resultados.nota_percentual
            FROM leituras_scan
            LEFT JOIN resultados ON resultados.id = leituras_scan.resultado_id
            WHERE leituras_scan.sessao_id = ?
              AND leituras_scan.aluno_id = ?
              AND leituras_scan.prova_id = ?
              AND leituras_scan.status = 'ok'
            ORDER BY leituras_scan.id DESC
            LIMIT 1
            """,
            (sessao_id, aluno_id, prova_id),
        ).fetchone()

        if leitura_confirmada:
            resumo = recalcular_sessao_scan(conn, sessao_id)
            conn.commit()
            return jsonify({
                "ok": True,
                "status": "ignorado",
                "duplicado": True,
                "registrar_ui": False,
                "mensagem": "Folha já corrigida nesta sessão. Retire esta folha e mostre a próxima.",
                "aluno": prova_corrigir["aluno_nome"],
                "prova": prova_corrigir["titulo"],
                "nota_percentual": leitura_confirmada["nota_percentual"],
                "latencia_ms": round((time.perf_counter() - inicio) * 1000, 2),
                "resumo": resumo,
            })

        gabarito_oficial = json.loads(prova_corrigir["gabarito_json"])
        mapa_alternativas = carregar_mapa_alternativas_linha(prova_corrigir)
        resultado = corrigir_imagem_web(
            caminho_gabarito_aluno=str(caminho),
            gabarito_oficial=gabarito_oficial,
            usar_ia=False,
            mapa_alternativas=mapa_alternativas,
            tipo_prova=prova_corrigir["tipo_prova"] if "tipo_prova" in prova_corrigir.keys() and prova_corrigir["tipo_prova"] else "A",
        )
        if dados_qr and not resultado.get("dados_qr"):
            resultado["dados_qr"] = dados_qr

        nota = float(resultado["resultado"]["nota_percentual"])
        processamento = resultado.get("processamento", {})
        omr = processamento.get("omr", {}) if isinstance(processamento, dict) else {}
        status_omr = omr.get("status_omr", "")
        confianca_baixa = status_omr in ["falha_calibracao", "sem_marcacoes_detectadas", "erro"]
        sem_qr = not bool(dados_qr and dados_qr.get("prova_id"))

        status_leitura = "revisao" if confianca_baixa or sem_qr else "ok"
        mensagem = mensagem_qr
        if confianca_baixa:
            mensagem = f"{mensagem} Leitura OMR precisa de revisão: {omr.get('mensagem', status_omr)}"

        latencia_ms = round((time.perf_counter() - inicio) * 1000, 2)
        resultado.setdefault("processamento", {})["scan"] = {
            "sessao_id": sessao_id,
            "latencia_ms": latencia_ms,
            "status_scan": status_leitura,
            "mensagem_scan": mensagem,
        }

        # Se a leitura ficou duvidosa, não deixe o mesmo frame gerar vários
        # registros de revisão em sequência. Isso também permite tentar de novo:
        # uma leitura em revisão não bloqueia uma futura leitura OK da mesma folha.
        if status_leitura == "revisao":
            ultima_revisao = conn.execute(
                """
                SELECT id, criado_em FROM leituras_scan
                WHERE sessao_id = ? AND aluno_id = ? AND prova_id = ? AND status = 'revisao'
                ORDER BY id DESC
                LIMIT 1
                """,
                (sessao_id, aluno_id, prova_id),
            ).fetchone()
            if ultima_revisao:
                try:
                    criada_em = datetime.strptime(ultima_revisao["criado_em"], "%Y-%m-%d %H:%M:%S")
                    segundos = (datetime.now() - criada_em).total_seconds()
                except Exception:
                    segundos = 0
                if segundos < 6:
                    resumo = recalcular_sessao_scan(conn, sessao_id)
                    conn.commit()
                    return jsonify({
                        "ok": True,
                        "status": "aguardando",
                        "registrar_ui": False,
                        "mensagem": "Leitura ainda instável. Segure a folha parada, aproxime um pouco e melhore a iluminação.",
                        "aluno": prova_corrigir["aluno_nome"],
                        "prova": prova_corrigir["titulo"],
                        "nota_percentual": nota,
                        "latencia_ms": latencia_ms,
                        "resumo": resumo,
                    })

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
            "tipo_prova": prova_corrigir["tipo_prova"] if "tipo_prova" in prova_corrigir.keys() and prova_corrigir["tipo_prova"] else "A",
            "revisoes": (omr.get("recortes_revisao", []) if isinstance(omr, dict) else []),
            "resumo": resumo,
        })
    except Exception as erro:
        return jsonify({
            "ok": False,
            "status": "erro",
            "mensagem": f"Erro ao processar o frame: {erro}",
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
    return render_template("scan_resumo.html", sessao=dados["sessao"], leituras=dados["leituras"], faltosos=dados.get("faltosos", []))



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
            SELECT resultados.*, provas.aluno_id, alunos.nome AS aluno_nome, alunos.turma, alunos.matricula
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

    conn_tmp = conectar()
    try:
        questoes_anuladas = _questoes_anuladas_avaliacao(conn_tmp, avaliacao_id, int(professor_atual_id()))
    finally:
        conn_tmp.close()

    # Mantém só o resultado mais recente por prova/aluno.
    vistos = set()
    unicos = []
    for r in resultados:
        chave = int(r["prova_id"])
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append(r)

    provas_corrigidas_ids = {int(r["prova_id"]) for r in unicos}
    faltosos = [p for p in provas if int(p["id"]) not in provas_corrigidas_ids]

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
        "faltosos": faltosos,
        "questoes_anuladas": questoes_anuladas,
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



def _recalcular_resultado_json(resultado_json, questao, resposta_impressa):
    """Atualiza uma questão revisada manualmente e recalcula a nota."""
    dados = carregar_json_seguro(resultado_json, {})
    q = str(int(questao))
    resposta_impressa = str(resposta_impressa or "NULA").upper()[:1]
    if resposta_impressa not in ALTERNATIVAS_ATLAS:
        resposta_impressa = "NULA"

    gabarito = _ordenar_mapa_questoes(dados.get("gabarito_professor_extraido", {}))
    mapa = dados.get("mapa_alternativas", {}) if isinstance(dados.get("mapa_alternativas", {}), dict) else {}
    mapa_q = mapa.get(q, {alt: alt for alt in ALTERNATIVAS_ATLAS}) if isinstance(mapa, dict) else {alt: alt for alt in ALTERNATIVAS_ATLAS}
    resposta_original = mapa_q.get(resposta_impressa, resposta_impressa) if resposta_impressa != "NULA" else "NULA"

    dados.setdefault("gabarito_aluno_impresso", {})[q] = resposta_impressa
    dados.setdefault("gabarito_aluno_extraido", {})[q] = resposta_original

    acertos = erros = anuladas = 0
    detalhes = []
    for questao_item, correta in gabarito.items():
        marcada_original = str(dados.get("gabarito_aluno_extraido", {}).get(str(questao_item), "NULA")).upper()
        correta = str(correta).upper()
        if marcada_original == "NULA":
            status = "anulada_ou_em_branco"
            anuladas += 1
        elif marcada_original == correta:
            status = "correta"
            acertos += 1
        else:
            status = "errada"
            erros += 1
        detalhe_mapa = (mapa.get(str(questao_item), {}) if isinstance(mapa, dict) else {}) or {}
        impresso = dados.get("gabarito_aluno_impresso", {}).get(str(questao_item), marcada_original)
        detalhes.append({
            "questao": str(questao_item),
            "resposta_correta": correta,
            "resposta_aluno": marcada_original,
            "resposta_aluno_original": marcada_original,
            "marcada_na_folha": impresso,
            "mapa": detalhe_mapa,
            "status": status,
        })

    total = len(gabarito)
    nota = round((acertos / total) * 100, 2) if total else 0
    dados["resultado"] = {
        "total_questoes_oficial": total,
        "acertos": acertos,
        "erros": erros,
        "anuladas_ou_em_branco": anuladas,
        "nota_percentual": nota,
        "detalhes": detalhes,
    }
    dados.setdefault("processamento", {})["revisao_manual"] = {
        "questao": q,
        "resposta_impressa": resposta_impressa,
        "resposta_original": resposta_original,
        "atualizado_em": agora_str(),
    }
    return dados, nota


@app.route("/api/resultado/<int:resultado_id>/corrigir-questao", methods=["POST"])
def api_corrigir_questao_resultado(resultado_id):
    payload = request.get_json(silent=True) or {}
    questao = payload.get("questao")
    resposta = payload.get("resposta")
    if not questao:
        return jsonify({"ok": False, "mensagem": "Questão não informada."}), 400

    conn = conectar()
    try:
        row = conn.execute(
            "SELECT * FROM resultados WHERE id = ? AND professor_id = ?",
            (resultado_id, int(professor_atual_id())),
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "mensagem": "Resultado não encontrado."}), 404
        novo_json, nota = _recalcular_resultado_json(row["resultado_json"], questao, resposta)
        conn.execute(
            "UPDATE resultados SET resultado_json = ?, nota_percentual = ?, status_confianca = ? WHERE id = ?",
            (json.dumps(novo_json, ensure_ascii=False), nota, "revisado", resultado_id),
        )
        conn.commit()
        return jsonify({"ok": True, "nota_percentual": nota, "resultado": novo_json.get("resultado", {})})
    finally:
        conn.close()


@app.route("/corrigir", methods=["POST"])
def corrigir():
    """
    Corrige uma ou várias folhas.

    Importante: a prova correta vem do QR Code da própria folha.
    O campo "Prova base" fica apenas como plano B, caso o QR não seja lido.
    Isso evita corrigir a folha da Ana com o gabarito do Carlos, por exemplo.
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
            dados_qr = ler_qrcode(str(caminho))
            prova_corrigir = prova_base

            if dados_qr and dados_qr.get("prova_id"):
                prova_qr = conn.execute(
                    "SELECT * FROM provas WHERE id = ? AND professor_id = ?", (int(dados_qr["prova_id"]), int(professor_atual_id()))
                ).fetchone()

                if prova_qr:
                    prova_corrigir = prova_qr
                else:
                    avisos.append(
                        f"QR de {arquivo.filename} apontou para uma prova que não existe mais. Usei a prova base."
                    )
            else:
                avisos.append(
                    f"Não consegui ler o QR de {arquivo.filename}. Usei a prova base selecionada."
                )

            gabarito_oficial = json.loads(prova_corrigir["gabarito_json"])

            try:
                mapa_alternativas = carregar_mapa_alternativas_linha(prova_corrigir)
                resultado = corrigir_imagem_web(
                    caminho_gabarito_aluno=str(caminho),
                    gabarito_oficial=gabarito_oficial,
                    mapa_alternativas=mapa_alternativas,
                    tipo_prova=prova_corrigir["tipo_prova"] if "tipo_prova" in prova_corrigir.keys() and prova_corrigir["tipo_prova"] else "A",
                )
            except Exception as erro:
                avisos.append(f"Erro ao corrigir {arquivo.filename}: {erro}")
                continue

            # Mantém o QR pré-lido no resultado para facilitar debug, mesmo quando o corretor não reler o QR.
            if dados_qr and not resultado.get("dados_qr"):
                resultado["dados_qr"] = dados_qr

            aluno_id = (
                int(dados_qr["aluno_id"])
                if dados_qr and dados_qr.get("aluno_id")
                else prova_corrigir["aluno_id"]
            )
            nota = resultado["resultado"]["nota_percentual"]

            conn.execute(
                """
                INSERT INTO resultados (professor_id, aluno_id, prova_id, nota_percentual, resultado_json, status_confianca, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    int(professor_atual_id()),
                    aluno_id,
                    prova_corrigir["id"],
                    nota,
                    json.dumps(resultado, ensure_ascii=False),
                    "confiavel" if resultado["resultado"].get("anuladas_ou_em_branco", 0) == 0 else "revisao",
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


# ---------------------------------------------------------------------------
# Ferramentas extras do Atlas: anotações, diário, checklist, kanban e testes
# ---------------------------------------------------------------------------
CHECKLIST_PADRAO_TCC = [
    ("Planejamento", "Definir tema do projeto"),
    ("Planejamento", "Escrever problema de pesquisa"),
    ("Planejamento", "Escrever justificativa"),
    ("Planejamento", "Definir objetivo geral"),
    ("Planejamento", "Definir objetivos específicos"),
    ("Documentação", "Introdução"),
    ("Documentação", "Referencial teórico"),
    ("Documentação", "Metodologia"),
    ("Documentação", "Desenvolvimento do sistema"),
    ("Documentação", "Testes e validação"),
    ("Documentação", "Resultados obtidos"),
    ("Documentação", "Conclusão"),
    ("Documentação", "Referências"),
    ("Apresentação", "Slides da apresentação"),
    ("Apresentação", "Roteiro de fala"),
    ("Apresentação", "Demonstração do sistema"),
]

STATUS_KANBAN = {
    "afazer": "A fazer",
    "andamento": "Em andamento",
    "concluido": "Concluído",
    "travado": "Travado",
}


def _professor_id_obrigatorio():
    pid = professor_atual_id()
    if not pid:
        raise RuntimeError("Professor não autenticado.")
    return int(pid)


def garantir_checklist_padrao(professor_id):
    """Cria os itens padrão do checklist apenas uma vez por professor."""
    conn = conectar()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS total FROM checklist_tcc WHERE professor_id = ?",
            (int(professor_id),),
        ).fetchone()["total"]
        if int(total or 0) == 0:
            agora = agora_str()
            for ordem, (categoria, titulo) in enumerate(CHECKLIST_PADRAO_TCC, start=1):
                conn.execute(
                    """
                    INSERT INTO checklist_tcc (professor_id, titulo, categoria, concluido, ordem, criado_em, atualizado_em)
                    VALUES (?, ?, ?, 0, ?, ?, ?)
                    """,
                    (int(professor_id), titulo, categoria, ordem, agora, agora),
                )
            conn.commit()
    finally:
        conn.close()


def contar_ferramentas(professor_id):
    conn = conectar()
    try:
        return {
            "anotacoes": conn.execute("SELECT COUNT(*) AS total FROM anotacoes WHERE professor_id = ?", (professor_id,)).fetchone()["total"],
            "diario": conn.execute("SELECT COUNT(*) AS total FROM diario_bordo WHERE professor_id = ?", (professor_id,)).fetchone()["total"],
            "checklist_pendentes": conn.execute("SELECT COUNT(*) AS total FROM checklist_tcc WHERE professor_id = ? AND concluido = 0", (professor_id,)).fetchone()["total"],
            "tarefas_abertas": conn.execute("SELECT COUNT(*) AS total FROM cronograma_tarefas WHERE professor_id = ? AND status != 'concluido'", (professor_id,)).fetchone()["total"],
        }
    finally:
        conn.close()


@app.route("/ferramentas")
def ferramentas_painel():
    pid = _professor_id_obrigatorio()
    garantir_checklist_padrao(pid)
    contadores = contar_ferramentas(pid)
    return render_template("ferramentas.html", contadores=contadores)


@app.route("/ferramentas/anotacoes", methods=["GET", "POST"])
def anotacoes():
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        if request.method == "POST":
            titulo = request.form.get("titulo", "").strip()
            conteudo = request.form.get("conteudo", "").strip()
            categoria = request.form.get("categoria", "Geral").strip() or "Geral"
            turma = request.form.get("turma", "").strip()
            avaliacao_id = request.form.get("avaliacao_id") or None
            importante = 1 if request.form.get("importante") else 0
            if not titulo or not conteudo:
                flash("Informe título e conteúdo da anotação.", "warning")
                return redirect(url_for("anotacoes"))
            conn.execute(
                """
                INSERT INTO anotacoes (professor_id, titulo, conteudo, categoria, avaliacao_id, turma, importante, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (pid, titulo, conteudo, categoria, avaliacao_id, turma, importante, agora_str(), agora_str()),
            )
            conn.commit()
            flash("Anotação salva.", "success")
            return redirect(url_for("anotacoes"))

        notas = conn.execute(
            """
            SELECT a.*, av.titulo AS avaliacao_titulo
            FROM anotacoes a
            LEFT JOIN avaliacoes av ON av.id = a.avaliacao_id AND av.professor_id = a.professor_id
            WHERE a.professor_id = ?
            ORDER BY a.importante DESC, COALESCE(a.atualizado_em, a.criado_em) DESC, a.id DESC
            """,
            (pid,),
        ).fetchall()
        avaliacoes = conn.execute(
            "SELECT id, titulo FROM avaliacoes WHERE professor_id = ? ORDER BY id DESC LIMIT 80",
            (pid,),
        ).fetchall()
        turmas = conn.execute(
            "SELECT DISTINCT turma FROM alunos WHERE professor_id = ? AND turma != '' ORDER BY turma",
            (pid,),
        ).fetchall()
        return render_template("anotacoes.html", notas=notas, avaliacoes=avaliacoes, turmas=turmas)
    finally:
        conn.close()


@app.route("/ferramentas/anotacoes/<int:nota_id>/editar", methods=["POST"])
def editar_anotacao(nota_id):
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        conn.execute(
            """
            UPDATE anotacoes
               SET titulo = ?, conteudo = ?, categoria = ?, turma = ?, importante = ?, atualizado_em = ?
             WHERE id = ? AND professor_id = ?
            """,
            (
                request.form.get("titulo", "").strip() or "Sem título",
                request.form.get("conteudo", "").strip(),
                request.form.get("categoria", "Geral").strip() or "Geral",
                request.form.get("turma", "").strip(),
                1 if request.form.get("importante") else 0,
                agora_str(),
                int(nota_id),
                pid,
            ),
        )
        conn.commit()
        flash("Anotação atualizada.", "success")
    finally:
        conn.close()
    return redirect(url_for("anotacoes"))


@app.route("/ferramentas/anotacoes/<int:nota_id>/excluir", methods=["POST"])
def excluir_anotacao(nota_id):
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        conn.execute("DELETE FROM anotacoes WHERE id = ? AND professor_id = ?", (int(nota_id), pid))
        conn.commit()
        flash("Anotação excluída.", "success")
    finally:
        conn.close()
    return redirect(url_for("anotacoes"))


@app.route("/ferramentas/diario", methods=["GET", "POST"])
def diario_bordo():
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        if request.method == "POST":
            data = request.form.get("data", "").strip() or datetime.now().strftime("%Y-%m-%d")
            atividade = request.form.get("atividade", "").strip()
            if not atividade:
                flash("Descreva a atividade realizada.", "warning")
                return redirect(url_for("diario_bordo"))
            conn.execute(
                """
                INSERT INTO diario_bordo (professor_id, data, atividade, responsavel, status, observacoes, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    data,
                    atividade,
                    request.form.get("responsavel", "").strip(),
                    request.form.get("status", "feito").strip() or "feito",
                    request.form.get("observacoes", "").strip(),
                    agora_str(),
                    agora_str(),
                ),
            )
            conn.commit()
            flash("Registro adicionado ao diário de bordo.", "success")
            return redirect(url_for("diario_bordo"))
        registros = conn.execute(
            "SELECT * FROM diario_bordo WHERE professor_id = ? ORDER BY data DESC, id DESC",
            (pid,),
        ).fetchall()
        return render_template("diario_bordo.html", registros=registros, hoje=datetime.now().strftime("%Y-%m-%d"))
    finally:
        conn.close()


@app.route("/ferramentas/diario/<int:registro_id>/excluir", methods=["POST"])
def excluir_diario(registro_id):
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        conn.execute("DELETE FROM diario_bordo WHERE id = ? AND professor_id = ?", (int(registro_id), pid))
        conn.commit()
        flash("Registro removido do diário.", "success")
    finally:
        conn.close()
    return redirect(url_for("diario_bordo"))


@app.route("/ferramentas/checklist", methods=["GET", "POST"])
def checklist_tcc():
    pid = _professor_id_obrigatorio()
    garantir_checklist_padrao(pid)
    conn = conectar()
    try:
        if request.method == "POST":
            titulo = request.form.get("titulo", "").strip()
            categoria = request.form.get("categoria", "TCC").strip() or "TCC"
            if not titulo:
                flash("Informe o item do checklist.", "warning")
                return redirect(url_for("checklist_tcc"))
            ordem = conn.execute(
                "SELECT COALESCE(MAX(ordem), 0) + 1 AS prox FROM checklist_tcc WHERE professor_id = ?",
                (pid,),
            ).fetchone()["prox"]
            conn.execute(
                """
                INSERT INTO checklist_tcc (professor_id, titulo, categoria, concluido, ordem, criado_em, atualizado_em)
                VALUES (?, ?, ?, 0, ?, ?, ?)
                """,
                (pid, titulo, categoria, int(ordem or 1), agora_str(), agora_str()),
            )
            conn.commit()
            flash("Item adicionado ao checklist.", "success")
            return redirect(url_for("checklist_tcc"))
        itens = conn.execute(
            "SELECT * FROM checklist_tcc WHERE professor_id = ? ORDER BY categoria, ordem, id",
            (pid,),
        ).fetchall()
        total = len(itens)
        concluidos = sum(1 for item in itens if int(item["concluido"] or 0) == 1)
        progresso = round((concluidos / total) * 100) if total else 0
        return render_template("checklist_tcc.html", itens=itens, progresso=progresso, total=total, concluidos=concluidos)
    finally:
        conn.close()


@app.route("/ferramentas/checklist/<int:item_id>/toggle", methods=["POST"])
def toggle_checklist(item_id):
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        item = conn.execute("SELECT concluido FROM checklist_tcc WHERE id = ? AND professor_id = ?", (int(item_id), pid)).fetchone()
        if item:
            novo = 0 if int(item["concluido"] or 0) else 1
            conn.execute("UPDATE checklist_tcc SET concluido = ?, atualizado_em = ? WHERE id = ? AND professor_id = ?", (novo, agora_str(), int(item_id), pid))
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for("checklist_tcc"))


@app.route("/ferramentas/checklist/<int:item_id>/excluir", methods=["POST"])
def excluir_checklist(item_id):
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        conn.execute("DELETE FROM checklist_tcc WHERE id = ? AND professor_id = ?", (int(item_id), pid))
        conn.commit()
        flash("Item removido.", "success")
    finally:
        conn.close()
    return redirect(url_for("checklist_tcc"))


@app.route("/ferramentas/cronograma", methods=["GET", "POST"])
def cronograma():
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        if request.method == "POST":
            titulo = request.form.get("titulo", "").strip()
            if not titulo:
                flash("Informe o título da tarefa.", "warning")
                return redirect(url_for("cronograma"))
            conn.execute(
                """
                INSERT INTO cronograma_tarefas (professor_id, titulo, descricao, responsavel, status, prioridade, prazo, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    titulo,
                    request.form.get("descricao", "").strip(),
                    request.form.get("responsavel", "").strip(),
                    request.form.get("status", "afazer").strip() or "afazer",
                    request.form.get("prioridade", "media").strip() or "media",
                    request.form.get("prazo", "").strip(),
                    agora_str(),
                    agora_str(),
                ),
            )
            conn.commit()
            flash("Tarefa adicionada ao cronograma.", "success")
            return redirect(url_for("cronograma"))
        rows = conn.execute(
            "SELECT * FROM cronograma_tarefas WHERE professor_id = ? ORDER BY prazo IS NULL, prazo, id DESC",
            (pid,),
        ).fetchall()
        colunas = {k: [] for k in STATUS_KANBAN.keys()}
        for row in rows:
            status = row["status"] if row["status"] in colunas else "afazer"
            colunas[status].append(row)
        return render_template("cronograma.html", colunas=colunas, status_labels=STATUS_KANBAN)
    finally:
        conn.close()


@app.route("/ferramentas/cronograma/<int:tarefa_id>/status", methods=["POST"])
def mudar_status_tarefa(tarefa_id):
    pid = _professor_id_obrigatorio()
    novo_status = request.form.get("status", "afazer")
    if novo_status not in STATUS_KANBAN:
        novo_status = "afazer"
    conn = conectar()
    try:
        conn.execute("UPDATE cronograma_tarefas SET status = ?, atualizado_em = ? WHERE id = ? AND professor_id = ?", (novo_status, agora_str(), int(tarefa_id), pid))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("cronograma"))


@app.route("/ferramentas/cronograma/<int:tarefa_id>/excluir", methods=["POST"])
def excluir_tarefa_cronograma(tarefa_id):
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        conn.execute("DELETE FROM cronograma_tarefas WHERE id = ? AND professor_id = ?", (int(tarefa_id), pid))
        conn.commit()
        flash("Tarefa removida.", "success")
    finally:
        conn.close()
    return redirect(url_for("cronograma"))


@app.route("/ferramentas/testes")
def central_testes():
    return render_template("central_testes.html")


@app.route("/api/testes/<teste>")
def api_testes(teste):
    try:
        if teste == "banco":
            conn = conectar()
            try:
                tabelas = conn.execute("SELECT COUNT(*) AS total FROM sqlite_master WHERE type='table'").fetchone()["total"]
            finally:
                conn.close()
            return jsonify(ok=True, mensagem=f"Banco conectado. Tabelas encontradas: {tabelas}.")
        if teste == "opencv":
            import cv2  # noqa: F401
            import numpy as np  # noqa: F401
            return jsonify(ok=True, mensagem=f"OpenCV carregado com sucesso. Versão: {cv2.__version__}.")
        if teste == "pdf":
            from reportlab.pdfgen import canvas  # noqa: F401
            return jsonify(ok=True, mensagem="ReportLab disponível para gerar PDFs.")
        if teste == "qr":
            img = qrcode.make("atlas-teste-qr")
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            tamanho = buffer.tell()
            return jsonify(ok=True, mensagem=f"QR Code gerado em memória com sucesso ({tamanho} bytes).")
        if teste == "ia":
            chave = os.getenv("GOOGLE_API_KEY", "").strip()
            modelo = os.getenv("MODELO_GEMINI", "gemini-2.0-flash")
            if chave:
                return jsonify(ok=True, mensagem=f"Chave Gemini configurada. Modelo padrão: {modelo}. Nenhuma chamada externa foi feita neste teste.")
            return jsonify(ok=False, mensagem="GOOGLE_API_KEY não configurada. O Atlas continua funcionando em modo econômico/sem IA."), 200
        return jsonify(ok=False, mensagem="Teste desconhecido."), 404
    except Exception as erro:
        return jsonify(ok=False, mensagem=str(erro)), 500


@app.route("/sobre")
def sobre_atlas():
    return render_template("sobre_atlas.html")


# ---------------------------------------------------------------------------
# Módulos avançados: anulação em massa, pré-teste, listas, OCR assistido,
# 2ª chamada, exportações e encerramento de ano letivo.
# ---------------------------------------------------------------------------

def _questoes_anuladas_avaliacao(conn, avaliacao_id: int, professor_id: int):
    linhas = conn.execute(
        """
        SELECT questao, motivo FROM questoes_anuladas
        WHERE avaliacao_id = ? AND professor_id = ?
        ORDER BY questao
        """,
        (int(avaliacao_id), int(professor_id)),
    ).fetchall()
    return {str(int(l["questao"])): (l["motivo"] or "") for l in linhas}


def _aplicar_anulacoes_resultado_json(resultado_json, questoes_anuladas):
    """Remove questões anuladas do denominador e recalcula a nota.

    A questão anulada deixa de contar para todos: não soma acerto, erro nem
    anulada/branco. O detalhe permanece no JSON com status
    'anulada_pelo_professor' para auditoria.
    """
    dados = carregar_json_seguro(resultado_json, {})
    anuladas = {str(int(q)) for q in (questoes_anuladas or {}).keys()}

    gabarito = _ordenar_mapa_questoes(dados.get("gabarito_professor_extraido", {}))
    respostas = dados.get("gabarito_aluno_extraido", {}) if isinstance(dados.get("gabarito_aluno_extraido", {}), dict) else {}
    impresso = dados.get("gabarito_aluno_impresso", {}) if isinstance(dados.get("gabarito_aluno_impresso", {}), dict) else {}
    mapa = dados.get("mapa_alternativas", {}) if isinstance(dados.get("mapa_alternativas", {}), dict) else {}

    acertos = erros = branco = 0
    total_valido = 0
    detalhes = []

    for questao_item, correta in gabarito.items():
        q = str(questao_item)
        correta = str(correta).upper()[:1]
        marcada_original = str(respostas.get(q, "NULA")).upper()[:1]
        marcada_folha = impresso.get(q, marcada_original)
        detalhe_mapa = mapa.get(q, {}) if isinstance(mapa, dict) else {}

        if q in anuladas:
            status = "anulada_pelo_professor"
            motivo = questoes_anuladas.get(q, "") if isinstance(questoes_anuladas, dict) else ""
        else:
            motivo = ""
            total_valido += 1
            if marcada_original == "N" or marcada_original not in ALTERNATIVAS_ATLAS:
                status = "anulada_ou_em_branco"
                branco += 1
                marcada_original = "NULA"
            elif marcada_original == correta:
                status = "correta"
                acertos += 1
            else:
                status = "errada"
                erros += 1

        detalhes.append({
            "questao": q,
            "resposta_correta": correta,
            "resposta_aluno": marcada_original if marcada_original in ALTERNATIVAS_ATLAS else "NULA",
            "resposta_aluno_original": marcada_original if marcada_original in ALTERNATIVAS_ATLAS else "NULA",
            "marcada_na_folha": marcada_folha,
            "mapa": detalhe_mapa,
            "status": status,
            "motivo_anulacao": motivo,
        })

    nota = round((acertos / total_valido) * 100, 2) if total_valido else 0
    dados["questoes_anuladas_professor"] = questoes_anuladas or {}
    dados["resultado"] = {
        "total_questoes_oficial": total_valido,
        "total_questoes_original": len(gabarito),
        "acertos": acertos,
        "erros": erros,
        "anuladas_ou_em_branco": branco,
        "questoes_anuladas_professor": len(anuladas),
        "nota_percentual": nota,
        "detalhes": detalhes,
    }
    dados.setdefault("processamento", {})["anulacao_em_massa"] = {
        "questoes": sorted(list(anuladas), key=lambda x: int(x)),
        "atualizado_em": agora_str(),
    }
    return dados, nota


def _recalcular_resultados_avaliacao(conn, avaliacao_id: int, professor_id: int):
    anuladas = _questoes_anuladas_avaliacao(conn, avaliacao_id, professor_id)
    resultados = conn.execute(
        """
        SELECT resultados.*
        FROM resultados
        JOIN provas ON provas.id = resultados.prova_id
        WHERE provas.avaliacao_id = ? AND resultados.professor_id = ?
        """,
        (int(avaliacao_id), int(professor_id)),
    ).fetchall()
    atualizados = 0
    for row in resultados:
        novo_json, nota = _aplicar_anulacoes_resultado_json(row["resultado_json"], anuladas)
        conn.execute(
            "UPDATE resultados SET resultado_json = ?, nota_percentual = ?, status_confianca = ? WHERE id = ? AND professor_id = ?",
            (json.dumps(novo_json, ensure_ascii=False), nota, "recalculado", int(row["id"]), int(professor_id)),
        )
        atualizados += 1
    return atualizados


@app.route("/avaliacao/<int:avaliacao_id>/anular-questao", methods=["POST"])
def anular_questao_massa(avaliacao_id):
    pid = _professor_id_obrigatorio()
    acao = request.form.get("acao", "anular")
    try:
        questao = int(request.form.get("questao") or 0)
    except Exception:
        questao = 0
    motivo = request.form.get("motivo", "").strip()

    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "warning")
        return redirect(url_for("home"))
    total = int(avaliacao["total_questoes"] or 0)
    if questao < 1 or questao > total:
        flash("Informe uma questão válida para anular/restaurar.", "warning")
        return redirect(url_for("relatorio_avaliacao", avaliacao_id=avaliacao_id))

    conn = conectar()
    try:
        if acao == "restaurar":
            conn.execute(
                "DELETE FROM questoes_anuladas WHERE professor_id = ? AND avaliacao_id = ? AND questao = ?",
                (pid, int(avaliacao_id), questao),
            )
            flash(f"Questão {questao} restaurada. As notas foram recalculadas.", "success")
        else:
            conn.execute(
                """
                INSERT INTO questoes_anuladas (professor_id, avaliacao_id, questao, motivo, criado_em)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(professor_id, avaliacao_id, questao)
                DO UPDATE SET motivo = excluded.motivo
                """,
                (pid, int(avaliacao_id), questao, motivo, agora_str()),
            )
            flash(f"Questão {questao} anulada em massa. As notas foram recalculadas automaticamente.", "success")
        atualizados = _recalcular_resultados_avaliacao(conn, avaliacao_id, pid)
        conn.commit()
        flash(f"{atualizados} resultado(s) atualizado(s).", "info")
    finally:
        conn.close()
    return redirect(url_for("relatorio_avaliacao", avaliacao_id=avaliacao_id))


@app.route("/avaliacao/<int:avaliacao_id>/preteste")
def preteste_folha(avaliacao_id):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "warning")
        return redirect(url_for("home"))
    return render_template("preteste_folha.html", avaliacao=montar_dados_avaliacao(avaliacao))


@app.route("/api/preteste-folha", methods=["POST"])
def api_preteste_folha():
    """Valida uma folha recém-impressa antes de aplicar para a turma."""
    try:
        if request.is_json:
            dados = request.get_json(silent=True) or {}
            caminho = salvar_frame_base64(dados.get("imagem"))
        else:
            arquivo = request.files.get("folha")
            if not arquivo:
                return jsonify(ok=False, mensagem="Envie uma foto da folha impressa."), 400
            caminho = salvar_upload_temporario(arquivo)

        import cv2
        imagem = ler_imagem(str(caminho))
        if imagem is None:
            return jsonify(ok=False, mensagem="Não consegui abrir a imagem recebida."), 400
        pontos, diag = localizar_marcadores_canto(imagem)
        qr = ler_qrcode(str(caminho))
        ok_marcadores = bool(pontos is not None and diag.get("aplicado"))
        ok_qr = bool(qr and qr.get("prova_id") and qr.get("aluno_id"))
        problemas = []
        if not ok_marcadores:
            problemas.append("Os 4 marcadores de canto não foram detectados com segurança.")
        if not ok_qr:
            problemas.append("O QR Code não foi lido com segurança.")
        return jsonify(
            ok=ok_marcadores and ok_qr,
            qr_ok=ok_qr,
            marcadores_ok=ok_marcadores,
            diagnostico_marcadores=diag,
            dados_qr=qr or {},
            mensagem=("Folha aprovada para impressão em lote." if ok_marcadores and ok_qr else "Ajuste a impressão antes de aplicar: " + " ".join(problemas)),
        )
    except Exception as erro:
        return jsonify(ok=False, mensagem=str(erro)), 500


def _carregar_questao_cache(row):
    alternativas = carregar_json_seguro(row["alternativas_json"], {})
    return {
        "id": row["id"],
        "numero": row.get("numero") if hasattr(row, "get") else None,
        "area": row["materia"],
        "tema": row["tema"],
        "dificuldade": row["dificuldade"],
        "habilidade": row["habilidade"] if "habilidade" in row.keys() else "",
        "contexto": row["contexto"] or "",
        "enunciado": row["enunciado"],
        "alternativas": {a: alternativas.get(a, "") for a in ALTERNATIVAS_ATLAS},
        "correta": row["correta"],
        "explicacao": row["explicacao"] or "",
    }


def gerar_pdf_lista_exercicios(titulo, questoes, incluir_gabarito=False):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas

    PDF_DIR = BASE_DIR / "static" / "pdfs"
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    caminho = PDF_DIR / f"lista_exercicios_{uuid.uuid4().hex[:10]}.pdf"

    c = canvas.Canvas(str(caminho), pagesize=A4)
    largura_pagina, altura_pagina = A4
    margem = 1.7 * cm
    largura_texto = largura_pagina - (2 * margem)
    y = altura_pagina - margem
    c.setTitle(titulo)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margem, y, titulo[:90])
    y -= 22
    c.setFont("Helvetica", 10)
    c.drawString(margem, y, "Lista de exercícios gerada pelo Atlas")
    y -= 22

    for i, q in enumerate(questoes, start=1):
        if y < 140:
            c.showPage(); y = altura_pagina - margem
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margem, y, f"Questão {i:02d} - {q.get('area','')}")
        y -= 14
        if q.get("contexto"):
            y = _texto_pdf(c, q.get("contexto"), margem, y, largura_texto, tamanho=9, entrelinha=12)
        y = _texto_pdf(c, q.get("enunciado"), margem, y, largura_texto, fonte="Helvetica-Bold", tamanho=10, entrelinha=13)
        alternativas = q.get("alternativas", {})
        for alt in ALTERNATIVAS_ATLAS:
            y = _texto_pdf(c, f"{alt}) {alternativas.get(alt,'')}", margem + 10, y, largura_texto - 10, tamanho=9, entrelinha=12)
        y -= 8

    if incluir_gabarito:
        c.showPage(); y = altura_pagina - margem
        c.setFont("Helvetica-Bold", 14)
        c.drawString(margem, y, "Gabarito")
        y -= 20
        c.setFont("Helvetica", 11)
        for i, q in enumerate(questoes, start=1):
            if y < 60:
                c.showPage(); y = altura_pagina - margem; c.setFont("Helvetica", 11)
            c.drawString(margem, y, f"{i:02d}. {q.get('correta','-')}")
            y -= 14
    c.save()
    return caminho


@app.route("/questoes/lista", methods=["GET", "POST"])
def lista_exercicios():
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        if request.method == "POST":
            ids = [int(x) for x in request.form.getlist("questao_id") if str(x).isdigit()]
            titulo = request.form.get("titulo", "Lista de Exercícios Atlas").strip() or "Lista de Exercícios Atlas"
            incluir_gabarito = 1 if request.form.get("incluir_gabarito") else 0
            if not ids:
                flash("Selecione pelo menos uma questão para gerar a lista.", "warning")
                return redirect(url_for("lista_exercicios"))
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM questoes_cache WHERE professor_id = ? AND id IN ({placeholders}) ORDER BY id",
                [pid, *ids],
            ).fetchall()
            questoes = [_carregar_questao_cache(r) for r in rows]
            caminho = gerar_pdf_lista_exercicios(titulo, questoes, bool(incluir_gabarito))
            conn.execute(
                """
                INSERT INTO listas_exercicios (professor_id, titulo, filtros_json, questoes_json, incluir_gabarito, arquivo_pdf, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pid, titulo, json.dumps(dict(request.form), ensure_ascii=False), json.dumps(questoes, ensure_ascii=False), incluir_gabarito, str(caminho), agora_str()),
            )
            conn.commit()
            return send_file(caminho, as_attachment=True, download_name=f"{secure_filename(titulo) or 'lista_exercicios'}.pdf")

        materia = request.args.get("materia", "").strip()
        tema = request.args.get("tema", "").strip()
        dificuldade = request.args.get("dificuldade", "").strip()
        params = [pid]
        where = ["professor_id = ?", "aprovado = 1"]
        if materia:
            where.append("LOWER(materia) LIKE ?"); params.append(f"%{materia.lower()}%")
        if tema:
            where.append("LOWER(tema) LIKE ?"); params.append(f"%{tema.lower()}%")
        if dificuldade:
            where.append("LOWER(dificuldade) = ?"); params.append(dificuldade.lower())
        questoes = conn.execute(
            f"SELECT * FROM questoes_cache WHERE {' AND '.join(where)} ORDER BY usado_vezes ASC, id DESC LIMIT 80",
            params,
        ).fetchall()
        listas = conn.execute("SELECT * FROM listas_exercicios WHERE professor_id = ? ORDER BY id DESC LIMIT 10", (pid,)).fetchall()
    finally:
        conn.close()
    return render_template("lista_exercicios.html", questoes=questoes, listas=listas, filtros=request.args)


def _normalizar_texto_busca(texto):
    return re.sub(r"\s+", " ", str(texto or "").strip().lower())


def _parsear_prova_colada(texto):
    """Parser conservador para provas antigas copiadas do Word/PDF."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    padrao_q = re.compile(r"(?m)^\s*(\d{1,3})[\).\-]\s+")
    matches = list(padrao_q.finditer(texto))
    questoes = []
    for idx, m in enumerate(matches):
        inicio = m.end()
        fim = matches[idx + 1].start() if idx + 1 < len(matches) else len(texto)
        bloco = texto[inicio:fim].strip()
        if len(bloco) < 20:
            continue
        alt_matches = list(re.finditer(r"(?im)^\s*([A-Ea-e])[\)\.\-]\s+", bloco))
        if len(alt_matches) < 2:
            continue
        enunciado = bloco[:alt_matches[0].start()].strip()
        alternativas = {}
        for j, am in enumerate(alt_matches):
            alt = am.group(1).upper()
            ai = am.end()
            af = alt_matches[j + 1].start() if j + 1 < len(alt_matches) else len(bloco)
            alternativas[alt] = bloco[ai:af].strip()
        for alt in ALTERNATIVAS_ATLAS:
            alternativas.setdefault(alt, "")
        questoes.append({"enunciado": enunciado, "alternativas": alternativas})
    return questoes


@app.route("/questoes/importar-prova", methods=["GET", "POST"])
def importar_prova_antiga():
    pid = _professor_id_obrigatorio()
    if request.method == "POST":
        texto = request.form.get("texto_prova", "")
        materia = request.form.get("materia", "Geral").strip() or "Geral"
        tema = request.form.get("tema", "").strip()
        dificuldade = request.form.get("dificuldade", "medio").strip().lower() or "medio"
        correta_padrao = request.form.get("correta_padrao", "A").strip().upper()[:1]
        if correta_padrao not in ALTERNATIVAS_ATLAS:
            correta_padrao = "A"
        questoes = _parsear_prova_colada(texto)
        conn = conectar(); salvas = 0
        try:
            for q in questoes:
                q["correta"] = correta_padrao
                h = hash_questao(q)
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO questoes_cache
                    (professor_id, materia, tema, dificuldade, modelo, contexto, enunciado, alternativas_json, correta, habilidade, explicacao, origem, hash, aprovado, usado_vezes, criado_em, atualizado_em)
                    VALUES (?, ?, ?, ?, 'Importada', '', ?, ?, ?, '', '', 'importada', ?, 1, 0, ?, ?)
                    """,
                    (pid, materia, tema, dificuldade, q["enunciado"], json.dumps(q["alternativas"], ensure_ascii=False), correta_padrao, h, agora_str(), agora_str()),
                )
                salvas += 1 if cur.rowcount else 0
            conn.commit()
        finally:
            conn.close()
        flash(f"Importação concluída: {salvas} questão(ões) salva(s) no banco local. Confira o gabarito depois.", "success")
        return redirect(url_for("lista_exercicios", materia=materia, tema=tema))
    return render_template("importar_prova_antiga.html")


@app.route("/avaliacao/<int:avaliacao_id>/segunda-chamada", methods=["POST"])
def gerar_segunda_chamada(avaliacao_id):
    pid = _professor_id_obrigatorio()
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação original não encontrada.", "warning")
        return redirect(url_for("home"))
    dados = montar_dados_avaliacao(avaliacao)
    total = int(request.form.get("total_questoes") or dados["total_questoes"] or 1)
    titulo = request.form.get("titulo", f"2ª Chamada - {dados['titulo']}").strip() or f"2ª Chamada - {dados['titulo']}"
    prova = gerar_prova_hibrida(
        titulo=titulo,
        materias=dados["materias"],
        temas=dados.get("temas") or "",
        total_questoes=total,
        especificacoes="Gerada como 2ª chamada, usando o banco local sempre que possível.",
        professor_id=pid,
        permitir_ia=False,
    )
    nova_id = inserir_avaliacao_gerada(prova, {"professor_id": pid})
    flash("2ª chamada gerada usando banco local/reserva, sem consumir IA.", "success")
    return redirect(url_for("ver_avaliacao", avaliacao_id=nova_id))


@app.route("/avaliacao/<int:avaliacao_id>/exportar-diario")
def exportar_diario_classe(avaliacao_id):
    rel = calcular_relatorio_avaliacao(avaliacao_id)
    if not rel:
        flash("Avaliação não encontrada.", "warning")
        return redirect(url_for("home"))
    saida = io.StringIO()
    writer = csv.writer(saida, delimiter=';')
    writer.writerow(["Matricula", "Nome", "Nota Final"])
    for item in rel["alunos_resultados"]:
        linha = item["linha"]
        writer.writerow([linha["matricula"] if "matricula" in linha.keys() else "", linha["aluno_nome"], str(linha["nota_percentual"]).replace('.', ',')])
    mem = io.BytesIO(saida.getvalue().encode("utf-8-sig"))
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=f"diario_avaliacao_{avaliacao_id}.csv")


def gerar_pdf_devolutivas(relatorio):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas
    PDF_DIR = BASE_DIR / "static" / "pdfs"; PDF_DIR.mkdir(parents=True, exist_ok=True)
    caminho = PDF_DIR / f"devolutivas_{relatorio['avaliacao']['id']}_{uuid.uuid4().hex[:8]}.pdf"
    c = canvas.Canvas(str(caminho), pagesize=A4)
    w, h = A4; margem = 1.2 * cm; y = h - margem
    c.setTitle("Devolutivas individuais")
    for idx, item in enumerate(relatorio["alunos_resultados"], start=1):
        if y < 5.2 * cm:
            c.showPage(); y = h - margem
        linha = item["linha"]; resumo = item["resumo"]
        c.roundRect(margem, y - 4.3*cm, w - 2*margem, 4.0*cm, 6, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 11); c.drawString(margem+10, y-18, "Atlas · Devolutiva individual")
        c.setFont("Helvetica", 9); c.drawString(margem+10, y-34, f"Avaliação: {relatorio['avaliacao']['titulo'][:80]}")
        c.drawString(margem+10, y-50, f"Aluno: {linha['aluno_nome']}  |  Turma: {linha['turma']}")
        c.setFont("Helvetica-Bold", 16); c.drawString(margem+10, y-78, f"Nota: {linha['nota_percentual']}%")
        c.setFont("Helvetica", 9); c.drawString(margem+130, y-78, f"Acertos: {resumo.get('acertos',0)} | Erros: {resumo.get('erros',0)} | Branco/Nulas: {resumo.get('anuladas_ou_em_branco',0)}")
        erradas = [str(d.get('questao')) for d in resumo.get('detalhes', []) if d.get('status') in ['errada', 'anulada_ou_em_branco']]
        c.drawString(margem+10, y-100, "Questões para revisar: " + (", ".join(erradas[:20]) if erradas else "nenhuma"))
        y -= 4.7 * cm
    c.save(); return caminho


@app.route("/avaliacao/<int:avaliacao_id>/devolutivas-pdf")
def devolutivas_pdf(avaliacao_id):
    rel = calcular_relatorio_avaliacao(avaliacao_id)
    if not rel:
        flash("Avaliação não encontrada.", "warning")
        return redirect(url_for("home"))
    caminho = gerar_pdf_devolutivas(rel)
    return send_file(caminho, as_attachment=True, download_name=f"devolutivas_avaliacao_{avaliacao_id}.pdf")


def _ocr_nome_cabecalho(caminho):
    """OCR opcional: usa pytesseract se estiver instalado; caso contrário retorna vazio."""
    try:
        import pytesseract  # dependência opcional
    except Exception:
        return "", "pytesseract não instalado; use confirmação manual."
    try:
        import cv2
        imagem = ler_imagem(str(caminho))
        if imagem is None:
            return "", "imagem inválida"
        h, w = imagem.shape[:2]
        cabecalho = imagem[0:int(h*0.25), 0:w]
        gray = cv2.cvtColor(cabecalho, cv2.COLOR_BGR2GRAY)
        texto = pytesseract.image_to_string(gray, lang="por")
        return texto.strip(), "OCR executado"
    except Exception as erro:
        return "", str(erro)


def _sugerir_alunos_por_texto(conn, professor_id, texto, turma=None, limite=5):
    alunos = conn.execute(
        "SELECT * FROM alunos WHERE professor_id = ? ORDER BY turma, nome",
        (int(professor_id),),
    ).fetchall()
    alvo = _normalizar_texto_busca(texto)
    sugestoes = []
    for a in alunos:
        base = _normalizar_texto_busca(f"{a['nome']} {a['matricula']} {a['turma']}")
        score = difflib.SequenceMatcher(None, alvo, base).ratio() if alvo else 0
        if str(a["nome"]).lower() in alvo:
            score += 0.5
        if str(a["matricula"]).lower() in alvo:
            score += 0.4
        sugestoes.append({"id": a["id"], "nome": a["nome"], "matricula": a["matricula"], "turma": a["turma"], "score": round(min(score, 1.0), 3)})
    return sorted(sugestoes, key=lambda x: x["score"], reverse=True)[:limite]


@app.route("/identificacao-assistida", methods=["GET", "POST"])
def identificacao_assistida():
    pid = _professor_id_obrigatorio()
    conn = conectar()
    try:
        if request.method == "POST":
            arquivo = request.files.get("folha")
            avaliacao_id = int(request.form.get("avaliacao_id") or 0) or None
            if not arquivo:
                flash("Envie uma foto da folha sem QR.", "warning")
                return redirect(url_for("identificacao_assistida"))
            caminho = salvar_upload_temporario(arquivo)
            texto, msg = _ocr_nome_cabecalho(caminho)
            sugestoes = _sugerir_alunos_por_texto(conn, pid, texto)
            conn.execute(
                """
                INSERT INTO identificacoes_pendentes
                (professor_id, avaliacao_id, imagem_arquivo, texto_detectado, sugestoes_json, status, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, 'pendente', ?, ?)
                """,
                (pid, avaliacao_id, str(caminho), texto, json.dumps(sugestoes, ensure_ascii=False), agora_str(), agora_str()),
            )
            conn.commit()
            flash(f"Identificação criada. {msg}", "info")
        avaliacoes = conn.execute("SELECT * FROM avaliacoes WHERE professor_id = ? AND COALESCE(arquivado,0)=0 ORDER BY id DESC", (pid,)).fetchall()
        pendencias = conn.execute("SELECT * FROM identificacoes_pendentes WHERE professor_id = ? ORDER BY id DESC LIMIT 20", (pid,)).fetchall()
    finally:
        conn.close()
    return render_template("identificacao_assistida.html", avaliacoes=avaliacoes, pendencias=pendencias, carregar_json_seguro=carregar_json_seguro)


@app.route("/identificacao-assistida/<int:pendencia_id>/confirmar", methods=["POST"])
def confirmar_identificacao_assistida(pendencia_id):
    pid = _professor_id_obrigatorio()
    aluno_id = int(request.form.get("aluno_id") or 0)
    conn = conectar()
    try:
        conn.execute(
            "UPDATE identificacoes_pendentes SET aluno_id_confirmado = ?, status = 'confirmada', atualizado_em = ? WHERE id = ? AND professor_id = ?",
            (aluno_id, agora_str(), int(pendencia_id), pid),
        )
        conn.commit()
        flash("Aluno confirmado para a folha pendente.", "success")
    finally:
        conn.close()
    return redirect(url_for("identificacao_assistida"))


@app.route("/ferramentas/encerramento", methods=["GET", "POST"])
def encerramento_ano_letivo():
    pid = _professor_id_obrigatorio()
    ano_padrao = str(datetime.now().year)
    conn = conectar()
    try:
        if request.method == "POST":
            ano = request.form.get("ano_letivo", ano_padrao).strip() or ano_padrao
            descricao = request.form.get("descricao", "").strip()
            totais = {
                "avaliacoes": conn.execute("SELECT COUNT(*) AS t FROM avaliacoes WHERE professor_id=? AND COALESCE(arquivado,0)=0", (pid,)).fetchone()["t"],
                "provas": conn.execute("SELECT COUNT(*) AS t FROM provas WHERE professor_id=? AND COALESCE(arquivado,0)=0", (pid,)).fetchone()["t"],
                "resultados": conn.execute("SELECT COUNT(*) AS t FROM resultados WHERE professor_id=? AND COALESCE(arquivado,0)=0", (pid,)).fetchone()["t"],
            }
            for tabela in ["avaliacoes", "provas", "resultados", "anotacoes", "diario_bordo", "cronograma_tarefas"]:
                conn.execute(f"UPDATE {tabela} SET arquivado = 1, ano_letivo = ? WHERE professor_id = ? AND COALESCE(arquivado,0)=0", (ano, pid))
            conn.execute(
                "INSERT INTO arquivos_ano_letivo (professor_id, ano_letivo, descricao, totais_json, criado_em) VALUES (?, ?, ?, ?, ?)",
                (pid, ano, descricao, json.dumps(totais, ensure_ascii=False), agora_str()),
            )
            conn.commit()
            flash(f"Ano letivo {ano} arquivado. O painel ficará mais limpo para o próximo ciclo.", "success")
        arquivos = conn.execute("SELECT * FROM arquivos_ano_letivo WHERE professor_id = ? ORDER BY id DESC", (pid,)).fetchall()
        totais_abertos = {
            "avaliacoes": conn.execute("SELECT COUNT(*) AS t FROM avaliacoes WHERE professor_id=? AND COALESCE(arquivado,0)=0", (pid,)).fetchone()["t"],
            "provas": conn.execute("SELECT COUNT(*) AS t FROM provas WHERE professor_id=? AND COALESCE(arquivado,0)=0", (pid,)).fetchone()["t"],
            "resultados": conn.execute("SELECT COUNT(*) AS t FROM resultados WHERE professor_id=? AND COALESCE(arquivado,0)=0", (pid,)).fetchone()["t"],
        }
    finally:
        conn.close()
    return render_template("encerramento_ano.html", arquivos=arquivos, totais_abertos=totais_abertos, ano_padrao=ano_padrao, carregar_json_seguro=carregar_json_seguro)


if __name__ == "__main__":
    iniciar_worker_fila()
    porta = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=porta, debug=False, use_reloader=False)
