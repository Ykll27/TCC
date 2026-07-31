import base64
import csv
import hashlib
import io
import json
import os
import random
import re
import statistics
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from difflib import SequenceMatcher

import qrcode
from flask import Flask, request, redirect, url_for, flash, render_template, send_file, Response, stream_with_context, jsonify, session, g
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from bd import iniciar_banco, conectar
from corretor import corrigir_imagem_web, ler_qrcode, comparar_respostas
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


def assinatura_qr(professor_id, aluno_id, prova_id, matricula=""):
    """Assinatura simples para impedir que um QR de outra conta/prova seja aceito por engano."""
    segredo = app.secret_key or "atlas-dev"
    base = f"{professor_id}:{aluno_id}:{prova_id}:{matricula}:{segredo}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:18]


def gerar_qr(aluno, prova_id, titulo, disciplina):
    professor_id = int(professor_atual_id() or aluno["professor_id"] or 1)
    dados = {
        "v": 2,
        "sistema": "atlas",
        "professor_id": professor_id,
        "aluno_id": int(aluno["id"]),
        "nome": aluno["nome"],
        "matricula": aluno["matricula"],
        "turma": aluno["turma"],
        "prova_id": int(prova_id),
        "titulo": titulo,
        "disciplina": disciplina,
    }
    dados["assinatura"] = assinatura_qr(professor_id, dados["aluno_id"], dados["prova_id"], dados["matricula"])

    nome = f"prova_{prova_id}_aluno_{aluno['id']}.png"
    caminho = QR_DIR / nome

    img = qrcode.make(json.dumps(dados, ensure_ascii=False))
    img.save(caminho)

    return nome


def validar_qr_folha(dados_qr, prova):
    """Valida se o QR realmente aponta para a folha/prova daquele professor.

    Retorna (ok, mensagem). QRs antigos sem assinatura continuam aceitos quando
    prova_id e aluno_id batem com o banco, para não inutilizar folhas antigas.
    """
    if not isinstance(dados_qr, dict):
        return False, "QR Code não foi lido. A folha foi enviada para identificação assistida."
    try:
        qr_prova_id = int(dados_qr.get("prova_id") or 0)
        qr_aluno_id = int(dados_qr.get("aluno_id") or 0)
    except Exception:
        return False, "QR Code incompleto ou inválido."
    if not prova:
        return False, "A prova apontada pelo QR não existe para este professor."
    if qr_prova_id != int(prova["id"]):
        return False, "QR Code aponta para outra prova."
    if qr_aluno_id != int(prova["aluno_id"]):
        return False, "QR Code aponta para aluno diferente da folha registrada."
    qr_professor = dados_qr.get("professor_id")
    if qr_professor and int(qr_professor) != int(prova["professor_id"]):
        return False, "QR Code pertence a outro professor."
    assinatura = dados_qr.get("assinatura")
    if assinatura:
        esperada = assinatura_qr(prova["professor_id"], prova["aluno_id"], prova["id"], prova["matricula"])
        if str(assinatura) != esperada:
            return False, "Assinatura do QR inválida."
    return True, "QR Code validado com segurança."


def caminho_relativo_upload(caminho: Path) -> str:
    try:
        return str(Path(caminho).resolve().relative_to(UPLOAD_DIR.resolve()))
    except Exception:
        return Path(caminho).name


def normalizar_texto_busca(texto: str) -> str:
    texto = str(texto or "").lower()
    texto = re.sub(r"[^a-z0-9áéíóúàâêôãõçñ ]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def gerar_sugestoes_alunos(conn, prova_base_id=None, texto_detectado=""):
    """Sugere alunos para identificação manual quando QR falha.

    Sem OCR pesado por padrão: usa a turma da prova base e similaridade simples
    com qualquer texto detectado/disponível. Se OCR não estiver configurado,
    ainda lista os alunos mais prováveis da mesma turma.
    """
    pid = int(professor_atual_id())
    turma = None
    if prova_base_id:
        base = buscar_prova_com_aluno(conn, int(prova_base_id))
        if base:
            turma = base["turma"]
    if turma:
        alunos = conn.execute("SELECT * FROM alunos WHERE professor_id = ? AND turma = ? ORDER BY nome", (pid, turma)).fetchall()
    else:
        alunos = conn.execute("SELECT * FROM alunos WHERE professor_id = ? ORDER BY turma, nome", (pid,)).fetchall()
    alvo = normalizar_texto_busca(texto_detectado)
    sugestoes = []
    for a in alunos:
        nome = normalizar_texto_busca(a["nome"])
        score = 0.35
        if alvo:
            score = SequenceMatcher(None, alvo, nome).ratio()
            if str(a["matricula"]).lower() in alvo:
                score = max(score, 0.95)
        sugestoes.append({"id": int(a["id"]), "nome": a["nome"], "matricula": a["matricula"], "turma": a["turma"], "score": round(score * 100, 1)})
    return sorted(sugestoes, key=lambda x: x["score"], reverse=True)[:8]


def registrar_identificacao_pendente(conn, caminho, origem, prova_base_id=None, sessao_id=None, mensagem="QR não lido", dados_qr=None):
    """Cria uma pendência em vez de salvar nota no aluno errado."""
    # Evita criar uma pendência a cada frame quando a mesma folha fica parada na câmera.
    recente = None
    if sessao_id:
        recente = conn.execute(
            """
            SELECT * FROM identificacoes_pendentes
            WHERE professor_id = ? AND sessao_id = ? AND status = 'pendente'
              AND datetime(criado_em) >= datetime('now', 'localtime', '-8 seconds')
            ORDER BY id DESC LIMIT 1
            """,
            (int(professor_atual_id()), int(sessao_id)),
        ).fetchone()
    if recente:
        return int(recente["id"])

    sugestoes = gerar_sugestoes_alunos(conn, prova_base_id=prova_base_id)
    cursor = conn.execute(
        """
        INSERT INTO identificacoes_pendentes
            (professor_id, sessao_id, prova_base_id, imagem_arquivo, origem, status, mensagem, dados_qr_json, sugestoes_json, criado_em)
        VALUES (?, ?, ?, ?, ?, 'pendente', ?, ?, ?, ?)
        """,
        (
            int(professor_atual_id()),
            int(sessao_id) if sessao_id else None,
            int(prova_base_id) if prova_base_id else None,
            caminho_relativo_upload(Path(caminho)),
            origem,
            mensagem,
            json.dumps(dados_qr or {}, ensure_ascii=False),
            json.dumps(sugestoes, ensure_ascii=False),
            agora_str(),
        ),
    )
    return int(cursor.lastrowid)


def gerar_ordem_questoes(total_questoes: int, aluno_id: int, avaliacao_id: int, embaralhar: bool) -> list:
    ordem = list(range(1, int(total_questoes) + 1))
    if embaralhar and len(ordem) > 1:
        rng = random.Random(f"atlas:{avaliacao_id}:{aluno_id}:{app.secret_key}")
        rng.shuffle(ordem)
    return ordem


def remapear_gabarito_por_ordem(gabarito_original: dict, ordem: list) -> dict:
    return {str(novo): str(gabarito_original.get(str(original), "A")).upper()[:1] for novo, original in enumerate(ordem, start=1)}


def questoes_em_ordem_para_prova(avaliacao_dict: dict, ordem: list) -> list:
    questoes = avaliacao_dict.get("questoes", []) or []
    por_numero = {int(q.get("numero", i + 1)): q for i, q in enumerate(questoes)}
    saida = []
    for novo, original in enumerate(ordem, start=1):
        q = dict(por_numero.get(int(original), {}))
        if not q:
            continue
        q["numero_original"] = int(original)
        q["numero"] = int(novo)
        saida.append(q)
    return saida


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
            colunas = set(row.keys())
            questoes.append(
                {
                    "numero": i,
                    "area": row["materia"],
                    "tema": row["tema"] or "",
                    "dificuldade": row["dificuldade"] or "medio",
                    "tipo_questao": row["tipo_questao"] if "tipo_questao" in colunas else "",
                    "contexto": row["contexto"] or "",
                    "elemento_visual": carregar_json_seguro(row["visual_json"], {}) if "visual_json" in colunas else {},
                    "dados_calculo": carregar_json_seguro(row["calculo_json"], {}) if "calculo_json" in colunas else {},
                    "enunciado": row["enunciado"],
                    "alternativas": alternativas,
                    "correta": str(row["correta"] or "A").upper()[:1],
                    "habilidade": row["habilidade"] or "",
                    "competencia": row["competencia"] if "competencia" in colunas else "",
                    "resolucao": carregar_json_seguro(row["resolucao_json"], []) if "resolucao_json" in colunas else [],
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
                     habilidade, explicacao, visual_json, calculo_json, resolucao_json, tipo_questao, competencia,
                     origem, hash, aprovado, usado_vezes, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
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
                    json.dumps(q.get("elemento_visual") or {}, ensure_ascii=False),
                    json.dumps(q.get("dados_calculo") or {}, ensure_ascii=False),
                    json.dumps(q.get("resolucao") or [], ensure_ascii=False),
                    str(q.get("tipo_questao") or ""),
                    str(q.get("competencia") or ""),
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
        "SELECT status, latencia_ms FROM leituras_scan WHERE sessao_id = ? AND status != 'duplicado'",
        (int(sessao_id),),
    ).fetchall()

    total = len(linhas)
    sucesso = sum(1 for l in linhas if l["status"] == "ok")
    revisao = sum(1 for l in linhas if l["status"] in ["revisao", "identificacao", "qr_falhou"])
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


def _garantir_espaco_pdf(c, y, minimo=120):
    from reportlab.lib.pagesizes import A4
    _, altura_pagina = A4
    if y < minimo:
        c.showPage()
        return altura_pagina - 50
    return y


def _desenhar_visual_pdf(c, visual, x, y, largura):
    """Desenha tabelas, gráficos simples e esquemas no PDF da prova sem depender de imagem externa."""
    if not isinstance(visual, dict) or not visual:
        return y

    y = _garantir_espaco_pdf(c, y, 135)
    tipo = str(visual.get("tipo") or "diagrama").lower()
    titulo = str(visual.get("titulo") or "Elemento visual")[:90]
    fonte = str(visual.get("fonte") or "Elaboração própria/Atlas")[:80]

    c.setFont("Helvetica-Bold", 9)
    c.drawString(x, y, titulo)
    y -= 12

    if tipo == "tabela":
        colunas = visual.get("colunas") if isinstance(visual.get("colunas"), list) else []
        linhas = visual.get("linhas") if isinstance(visual.get("linhas"), list) else []
        if colunas:
            c.setFont("Helvetica-Bold", 8)
            col_w = largura / max(len(colunas), 1)
            for idx, col in enumerate(colunas):
                c.rect(x + idx * col_w, y - 4, col_w, 14, stroke=1, fill=0)
                c.drawString(x + idx * col_w + 3, y, str(col)[:22])
            y -= 14
        c.setFont("Helvetica", 8)
        col_count = max(len(colunas), 1)
        col_w = largura / col_count
        for linha in linhas[:7]:
            if not isinstance(linha, list):
                linha = [linha]
            y = _garantir_espaco_pdf(c, y, 80)
            for idx in range(col_count):
                valor = str(linha[idx] if idx < len(linha) else "")[:24]
                c.rect(x + idx * col_w, y - 4, col_w, 13, stroke=1, fill=0)
                c.drawString(x + idx * col_w + 3, y, valor)
            y -= 13
        y -= 5
    elif tipo in ["grafico_barras", "grafico_linha"]:
        dados = visual.get("dados") if isinstance(visual.get("dados"), list) else []
        maxv = max([float(d.get("valor") or 0) for d in dados if isinstance(d, dict)] or [1])
        c.setFont("Helvetica", 8)
        for d in dados[:7]:
            if not isinstance(d, dict):
                continue
            rotulo = str(d.get("rotulo") or "Item")[:18]
            valor = float(d.get("valor") or 0)
            bar_w = (valor / maxv) * (largura - 95) if maxv else 0
            y = _garantir_espaco_pdf(c, y, 80)
            c.drawString(x, y, rotulo)
            c.rect(x + 70, y - 3, bar_w, 8, stroke=1, fill=0)
            c.drawString(x + 75 + bar_w, y, str(round(valor, 2)))
            y -= 13
        y -= 4
    else:
        desc = str(visual.get("descricao") or "Figura esquemática gerada pelo Atlas.")
        y = _texto_pdf(c, "Figura/diagrama: " + desc, x, y, largura, tamanho=8, entrelinha=11)
        itens = visual.get("itens") if isinstance(visual.get("itens"), list) else []
        if itens:
            y = _texto_pdf(c, "Fluxo: " + " → ".join(str(i) for i in itens[:6]), x + 8, y, largura - 8, tamanho=8, entrelinha=11)

    c.setFont("Helvetica-Oblique", 7)
    c.drawString(x, y, f"Fonte: {fonte}")
    return y - 12


def _texto_calculo_pdf(q):
    calc = q.get("dados_calculo") if isinstance(q.get("dados_calculo"), dict) else {}
    if not calc:
        return ""
    partes = []
    if calc.get("formula"):
        partes.append(f"Fórmula/dado de apoio: {calc.get('formula')}")
    valores = calc.get("valores") if isinstance(calc.get("valores"), dict) else {}
    if valores:
        partes.append("Valores: " + "; ".join(f"{k}={v}" for k, v in list(valores.items())[:6]))
    return " | ".join(partes)


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
            texto_explicacao = f"{str(q.get('numero')).zfill(2)}. {q.get('explicacao', '')}"
            resolucao = q.get("resolucao") if isinstance(q.get("resolucao"), list) else []
            if resolucao:
                texto_explicacao += " Resolução: " + " | ".join(str(p) for p in resolucao[:5])
            y = _texto_pdf(
                c,
                texto_explicacao,
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
            y = _desenhar_visual_pdf(c, q.get("elemento_visual"), margem, y, largura_texto)
            apoio_calculo = _texto_calculo_pdf(q)
            if apoio_calculo:
                y = _texto_pdf(c, apoio_calculo, margem, y, largura_texto, fonte="Helvetica-Oblique", tamanho=8, entrelinha=11)
                y -= 2
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
    qr_arquivo = gerar_qr(aluno, prova_id, titulo, disciplina)

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





@app.route("/prova/<int:prova_id>/imprimir")
def imprimir_prova_personalizada(prova_id):
    conn = conectar()
    try:
        prova = conn.execute(
            """
            SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
            FROM provas
            JOIN alunos ON alunos.id = provas.aluno_id
            WHERE provas.id = ? AND provas.professor_id = ?
            """,
            (int(prova_id), int(professor_atual_id())),
        ).fetchone()
        if not prova:
            flash("Prova personalizada não encontrada.", "warning")
            return redirect(url_for("home"))
        avaliacao = carregar_avaliacao(int(prova["avaliacao_id"])) if prova["avaliacao_id"] else None
    finally:
        conn.close()
    if not avaliacao:
        flash("A avaliação original não foi encontrada.", "warning")
        return redirect(url_for("home"))
    dados_avaliacao = montar_dados_avaliacao(avaliacao)
    ordem = carregar_json_seguro(prova["ordem_questoes_json"], []) if "ordem_questoes_json" in prova.keys() else []
    if not ordem:
        ordem = list(range(1, int(prova["total_questoes"] or dados_avaliacao["total_questoes"]) + 1))
    questoes = questoes_em_ordem_para_prova(dados_avaliacao, ordem)
    return render_template("prova_personalizada.html", prova=prova, avaliacao=dados_avaliacao, questoes=questoes, ordem=ordem)


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
    preferencias_qualidade = []
    if request.form.get("questoes_completas_auto") == "on":
        preferencias_qualidade.append("Gerar questões mais completas, contextualizadas e menos rasas.")
    if request.form.get("usar_calculos_auto") == "on":
        preferencias_qualidade.append("Incluir cálculos, fórmulas, dados numéricos e resolução passo a passo quando fizer sentido.")
    if request.form.get("usar_visuais_auto") == "on":
        preferencias_qualidade.append("Incluir elementos visuais renderizáveis pelo Atlas, como tabelas, gráficos, esquemas e diagramas próprios, sem links externos.")
    if preferencias_qualidade:
        especificacoes = (especificacoes + "\n" if especificacoes else "") + "Preferências de qualidade: " + " ".join(preferencias_qualidade)
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
        gabarito_original = dados_avaliacao["gabarito_professor"]
        embaralhar_questoes = request.form.get("embaralhar_questoes") == "on"
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

            ordem_questoes = gerar_ordem_questoes(dados_avaliacao["total_questoes"], aluno_id, avaliacao_id, embaralhar_questoes)
            gabarito_folha = remapear_gabarito_por_ordem(gabarito_original, ordem_questoes)
            mapa_questoes = {str(novo): int(original) for novo, original in enumerate(ordem_questoes, start=1)}
            cursor = conn.execute(
                """
                INSERT INTO provas
                    (professor_id, titulo, disciplina, aluno_id, total_questoes, gabarito_json, qr_arquivo, criado_em, avaliacao_id, ordem_questoes_json, mapa_questoes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(professor_atual_id()),
                    dados_avaliacao["titulo"],
                    dados_avaliacao["materias"],
                    aluno_id,
                    dados_avaliacao["total_questoes"],
                    json.dumps(gabarito_folha, ensure_ascii=False),
                    None,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    avaliacao_id,
                    json.dumps(ordem_questoes, ensure_ascii=False),
                    json.dumps(mapa_questoes, ensure_ascii=False),
                ),
            )
            prova_id = cursor.lastrowid
            qr_arquivo = gerar_qr(aluno, prova_id, dados_avaliacao["titulo"], dados_avaliacao["materias"])
            conn.execute("UPDATE provas SET qr_arquivo = ? WHERE id = ?", (qr_arquivo, prova_id))

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
            "UPDATE sessoes_scan SET status = 'finalizada', finalizado_em = ? WHERE id = ? AND professor_id = ?",
            (agora_str(), sessao_id, int(professor_atual_id())),
        )
        resumo = recalcular_sessao_scan(conn, sessao_id)
        conn.commit()
        return jsonify({"ok": True, "sessao_id": sessao_id, "resumo": resumo})
    finally:
        conn.close()


@app.route("/scan/processar-frame", methods=["POST"])
def processar_frame_scan_rota():
    """Recebe um frame da câmera.

    Regra de segurança: QR falhou => NÃO salva resultado no aluno/prova base.
    A imagem vira uma pendência de identificação assistida.
    """
    dados = request.get_json(silent=True) or {}

    try:
        sessao_id = int(dados.get("sessao_id") or 0)
        prova_base_id = int(dados.get("prova_id") or dados.get("prova_base_id") or 0)
    except Exception:
        return jsonify({"ok": False, "status": "erro", "mensagem": "Sessão ou prova inválida."}), 400

    inicio = time.perf_counter()
    try:
        caminho = salvar_frame_base64(dados.get("imagem"))
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
        if not dados_qr or not dados_qr.get("prova_id"):
            latencia_ms = round((time.perf_counter() - inicio) * 1000, 2)
            pendente_id = registrar_identificacao_pendente(
                conn, caminho, origem="scan", prova_base_id=prova_base_id, sessao_id=sessao_id,
                mensagem="QR Code não foi lido. Confirme manualmente antes de salvar a nota.", dados_qr=dados_qr,
            )
            # Registra como revisão da sessão, sem resultado e sem aluno forçado.
            conn.execute(
                """
                INSERT INTO leituras_scan
                    (sessao_id, aluno_id, prova_id, resultado_id, nota_percentual, latencia_ms, status, mensagem, criado_em)
                VALUES (?, NULL, ?, NULL, NULL, ?, 'identificacao', ?, ?)
                """,
                (sessao_id, prova_base_id, latencia_ms, f"QR falhou. Pendência #{pendente_id} criada.", agora_str()),
            )
            resumo = recalcular_sessao_scan(conn, sessao_id)
            conn.commit()
            return jsonify({
                "ok": True,
                "status": "identificacao",
                "mensagem": "QR não lido. Não salvei nota. Abra Identificação Assistida para confirmar o aluno.",
                "pendente_id": pendente_id,
                "url_pendente": url_for("identificacoes_pendentes"),
                "aluno": "Identificação pendente",
                "prova": prova_base["titulo"],
                "nota_percentual": None,
                "latencia_ms": latencia_ms,
                "resumo": resumo,
            })

        prova_corrigir = buscar_prova_com_aluno(conn, int(dados_qr["prova_id"]))
        ok_qr, msg_qr = validar_qr_folha(dados_qr, prova_corrigir)
        if not ok_qr:
            latencia_ms = round((time.perf_counter() - inicio) * 1000, 2)
            pendente_id = registrar_identificacao_pendente(
                conn, caminho, origem="scan", prova_base_id=prova_base_id, sessao_id=sessao_id,
                mensagem=msg_qr, dados_qr=dados_qr,
            )
            conn.execute(
                """
                INSERT INTO leituras_scan
                    (sessao_id, aluno_id, prova_id, resultado_id, nota_percentual, latencia_ms, status, mensagem, criado_em)
                VALUES (?, NULL, ?, NULL, NULL, ?, 'identificacao', ?, ?)
                """,
                (sessao_id, prova_base_id, latencia_ms, f"{msg_qr} Pendência #{pendente_id} criada.", agora_str()),
            )
            resumo = recalcular_sessao_scan(conn, sessao_id)
            conn.commit()
            return jsonify({
                "ok": True,
                "status": "identificacao",
                "mensagem": msg_qr + " Não salvei nota; confirme manualmente.",
                "pendente_id": pendente_id,
                "url_pendente": url_for("identificacoes_pendentes"),
                "aluno": "Identificação pendente",
                "prova": prova_base["titulo"],
                "nota_percentual": None,
                "latencia_ms": latencia_ms,
                "resumo": resumo,
            })

        aluno_id = int(prova_corrigir["aluno_id"])
        prova_id = int(prova_corrigir["id"])

        leitura_existente = conn.execute(
            """
            SELECT leituras_scan.*, resultados.nota_percentual
            FROM leituras_scan
            LEFT JOIN resultados ON resultados.id = leituras_scan.resultado_id
            WHERE leituras_scan.sessao_id = ? AND leituras_scan.aluno_id = ? AND leituras_scan.prova_id = ? AND leituras_scan.status != 'duplicado'
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
                "aluno": prova_corrigir["aluno_nome"],
                "prova": prova_corrigir["titulo"],
                "nota_percentual": leitura_existente["nota_percentual"],
                "latencia_ms": round((time.perf_counter() - inicio) * 1000, 2),
                "resumo": resumo,
            })

        gabarito_oficial = json.loads(prova_corrigir["gabarito_json"])
        resultado = corrigir_imagem_web(
            caminho_gabarito_aluno=str(caminho),
            gabarito_oficial=gabarito_oficial,
            usar_ia=False,
        )
        resultado["dados_qr"] = dados_qr
        resultado.setdefault("processamento", {})["validacao_qr"] = msg_qr
        if "mapa_questoes_json" in prova_corrigir.keys():
            resultado["mapa_questoes"] = carregar_json_seguro(prova_corrigir["mapa_questoes_json"], {})

        nota = float(resultado["resultado"]["nota_percentual"])
        processamento = resultado.get("processamento", {})
        omr = processamento.get("omr", {}) if isinstance(processamento, dict) else {}
        status_omr = omr.get("status_omr", "")
        confianca_baixa = status_omr in ["falha_calibracao", "sem_marcacoes_detectadas", "erro"]
        status_leitura = "revisao" if confianca_baixa else "ok"
        mensagem = msg_qr
        if confianca_baixa:
            mensagem = f"{mensagem} Leitura OMR precisa de revisão: {omr.get('mensagem', status_omr)}"

        latencia_ms = round((time.perf_counter() - inicio) * 1000, 2)
        resultado.setdefault("processamento", {})["scan"] = {
            "sessao_id": sessao_id,
            "latencia_ms": latencia_ms,
            "status_scan": status_leitura,
            "mensagem_scan": mensagem,
        }

        cursor = conn.execute(
            """
            INSERT INTO resultados (professor_id, aluno_id, prova_id, nota_percentual, resultado_json, status_confianca, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(professor_atual_id()), aluno_id, prova_id, nota,
                json.dumps(resultado, ensure_ascii=False), status_leitura, agora_str(),
            ),
        )
        resultado_id = cursor.lastrowid

        conn.execute(
            """
            INSERT INTO leituras_scan
                (sessao_id, aluno_id, prova_id, resultado_id, nota_percentual, latencia_ms, status, mensagem, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sessao_id, aluno_id, prova_id, resultado_id, nota, latencia_ms, status_leitura, mensagem, agora_str()),
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
            "url_revisao": url_for("revisar_resultado", resultado_id=resultado_id),
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
            antigo = dados["questoes"][i - 1] if i - 1 < len(dados.get("questoes", [])) else {}
            visual_txt = request.form.get(f"q{i}_elemento_visual_json", "").strip()
            calculo_txt = request.form.get(f"q{i}_dados_calculo_json", "").strip()
            try:
                elemento_visual = json.loads(visual_txt) if visual_txt else (antigo.get("elemento_visual") or {})
            except Exception:
                elemento_visual = antigo.get("elemento_visual") or {}
            try:
                dados_calculo = json.loads(calculo_txt) if calculo_txt else (antigo.get("dados_calculo") or {})
            except Exception:
                dados_calculo = antigo.get("dados_calculo") or {}
            resolucao_txt = request.form.get(f"q{i}_resolucao", "").strip()
            resolucao = [p.strip() for p in resolucao_txt.split("\n") if p.strip()] if resolucao_txt else (antigo.get("resolucao") or [])
            questoes.append({
                "numero": i,
                "area": request.form.get(f"q{i}_area", "").strip(),
                "tema": request.form.get(f"q{i}_tema", "").strip(),
                "dificuldade": request.form.get(f"q{i}_dificuldade", antigo.get("dificuldade", "medio")).strip() or "medio",
                "tipo_questao": request.form.get(f"q{i}_tipo_questao", antigo.get("tipo_questao", "")).strip(),
                "habilidade": request.form.get(f"q{i}_habilidade", "").strip(),
                "competencia": request.form.get(f"q{i}_competencia", antigo.get("competencia", "")).strip(),
                "contexto": request.form.get(f"q{i}_contexto", "").strip(),
                "elemento_visual": elemento_visual,
                "dados_calculo": dados_calculo,
                "enunciado": request.form.get(f"q{i}_enunciado", "").strip(),
                "alternativas": alts,
                "correta": correta,
                "resolucao": resolucao,
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



def corrigir_pendente_com_prova(conn, pendente, prova):
    caminho = UPLOAD_DIR / pendente["imagem_arquivo"]
    if not caminho.exists():
        raise RuntimeError("Imagem pendente não encontrada no servidor.")
    gabarito_oficial = json.loads(prova["gabarito_json"])
    resultado = corrigir_imagem_web(str(caminho), gabarito_oficial, usar_ia="auto")
    resultado.setdefault("processamento", {})["identificacao_assistida"] = {
        "pendente_id": int(pendente["id"]),
        "confirmado_em": agora_str(),
        "observacao": "QR falhou ou ficou inválido; professor confirmou a folha manualmente.",
    }
    if "mapa_questoes_json" in prova.keys():
        resultado["mapa_questoes"] = carregar_json_seguro(prova["mapa_questoes_json"], {})
    nota = float(resultado["resultado"]["nota_percentual"])
    cursor = conn.execute(
        """
        INSERT INTO resultados (professor_id, aluno_id, prova_id, nota_percentual, resultado_json, status_confianca, criado_em)
        VALUES (?, ?, ?, ?, ?, 'revisao_manual_qr', ?)
        """,
        (int(professor_atual_id()), int(prova["aluno_id"]), int(prova["id"]), nota, json.dumps(resultado, ensure_ascii=False), agora_str()),
    )
    resultado_id = int(cursor.lastrowid)
    conn.execute(
        """
        UPDATE identificacoes_pendentes
        SET status = 'resolvida', resultado_id = ?, resolvido_em = ?
        WHERE id = ? AND professor_id = ?
        """,
        (resultado_id, agora_str(), int(pendente["id"]), int(professor_atual_id())),
    )
    return resultado_id


@app.route("/identificacao-pendente")
def identificacoes_pendentes():
    conn = conectar()
    try:
        pendentes = conn.execute(
            """
            SELECT ip.*, provas.titulo AS prova_base_titulo
            FROM identificacoes_pendentes ip
            LEFT JOIN provas ON provas.id = ip.prova_base_id
            WHERE ip.professor_id = ? AND ip.status = 'pendente'
            ORDER BY ip.id DESC
            """,
            (int(professor_atual_id()),),
        ).fetchall()
    finally:
        conn.close()
    return render_template("identificacoes_pendentes.html", pendentes=pendentes)


@app.route("/identificacao-pendente/<int:pendente_id>", methods=["GET", "POST"])
def resolver_identificacao_pendente(pendente_id):
    conn = conectar()
    try:
        pendente = conn.execute(
            "SELECT * FROM identificacoes_pendentes WHERE id = ? AND professor_id = ?",
            (int(pendente_id), int(professor_atual_id())),
        ).fetchone()
        if not pendente:
            flash("Pendência não encontrada.", "warning")
            return redirect(url_for("identificacoes_pendentes"))

        if request.method == "POST":
            prova_id = int(request.form.get("prova_id") or 0)
            prova = buscar_prova_com_aluno(conn, prova_id)
            if not prova:
                flash("Escolha uma folha/prova válida para salvar a correção.", "warning")
                return redirect(url_for("resolver_identificacao_pendente", pendente_id=pendente_id))
            resultado_id = corrigir_pendente_com_prova(conn, pendente, prova)
            conn.commit()
            flash("Folha identificada e corrigida com revisão manual de QR.", "success")
            return redirect(url_for("ver_resultado_detalhado", resultado_id=resultado_id))

        sugestoes = carregar_json_seguro(pendente["sugestoes_json"], [])
        prova_base = buscar_prova_com_aluno(conn, int(pendente["prova_base_id"])) if pendente["prova_base_id"] else None
        if prova_base and prova_base["avaliacao_id"]:
            provas = conn.execute(
                """
                SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
                FROM provas JOIN alunos ON alunos.id = provas.aluno_id
                WHERE provas.professor_id = ? AND provas.avaliacao_id = ?
                ORDER BY alunos.turma, alunos.nome
                """,
                (int(professor_atual_id()), int(prova_base["avaliacao_id"])),
            ).fetchall()
        else:
            provas = conn.execute(
                """
                SELECT provas.*, alunos.nome AS aluno_nome, alunos.matricula, alunos.turma
                FROM provas JOIN alunos ON alunos.id = provas.aluno_id
                WHERE provas.professor_id = ?
                ORDER BY provas.id DESC LIMIT 200
                """,
                (int(professor_atual_id()),),
            ).fetchall()
    finally:
        conn.close()
    return render_template("resolver_identificacao.html", pendente=pendente, sugestoes=sugestoes, provas=provas)


@app.route("/uploads/<path:nome>")
def ver_upload(nome):
    caminho = (UPLOAD_DIR / nome).resolve()
    if not str(caminho).startswith(str(UPLOAD_DIR.resolve())) or not caminho.exists():
        return "Arquivo não encontrado", 404
    return send_file(caminho)


@app.route("/resultado/<int:resultado_id>/revisar", methods=["GET", "POST"])
def revisar_resultado(resultado_id):
    resultado = carregar_resultado_detalhado(resultado_id)
    if not resultado:
        flash("Resultado não encontrado.", "warning")
        return redirect(url_for("home"))
    dados = carregar_json_seguro(resultado["resultado_json"], {})
    gabarito = carregar_json_seguro(resultado["gabarito_json"], {})
    gabarito = _ordenar_mapa_questoes(gabarito)

    if request.method == "POST":
        respostas = {}
        for q in gabarito.keys():
            alt = str(request.form.get(f"q{q}", "NULA")).upper().strip()
            if alt not in ["A", "B", "C", "D", "E", "NULA"]:
                alt = "NULA"
            respostas[str(q)] = alt
        novo_resumo = comparar_respostas(gabarito, respostas)
        dados["gabarito_aluno_extraido"] = respostas
        dados["resultado"] = novo_resumo
        dados.setdefault("processamento", {})["revisao_manual"] = {
            "revisado_em": agora_str(),
            "observacao": request.form.get("observacao", "").strip(),
        }
        conn = conectar()
        try:
            conn.execute(
                """
                UPDATE resultados
                SET nota_percentual = ?, resultado_json = ?, status_confianca = 'revisado_manual', revisado_em = ?, observacao_revisao = ?
                WHERE id = ? AND professor_id = ?
                """,
                (novo_resumo["nota_percentual"], json.dumps(dados, ensure_ascii=False), agora_str(), request.form.get("observacao", "").strip(), int(resultado_id), int(professor_atual_id())),
            )
            conn.commit()
        finally:
            conn.close()
        flash("Resultado revisado e nota recalculada.", "success")
        return redirect(url_for("ver_resultado_detalhado", resultado_id=resultado_id))

    return render_template("revisar_resultado.html", resultado=resultado, dados=dados, gabarito=gabarito)


def diagnosticar_avaliacao(dados):
    diagnosticos = []
    for q in dados.get("questoes", []) or []:
        numero = q.get("numero")
        alts = q.get("alternativas", {}) if isinstance(q.get("alternativas"), dict) else {}
        if len(str(q.get("enunciado", "")).strip()) < 20:
            diagnosticos.append({"questao": numero, "tipo": "Enunciado curto", "mensagem": "Pode ficar pouco claro para o aluno."})
        vazias = [a for a in ["A", "B", "C", "D", "E"] if not str(alts.get(a, "")).strip()]
        if vazias:
            diagnosticos.append({"questao": numero, "tipo": "Alternativa vazia", "mensagem": "Faltam alternativas: " + ", ".join(vazias)})
        textos = [normalizar_texto_busca(alts.get(a, "")) for a in ["A", "B", "C", "D", "E"]]
        if len([t for t in textos if t]) != len(set([t for t in textos if t])):
            diagnosticos.append({"questao": numero, "tipo": "Alternativas repetidas", "mensagem": "Existem alternativas iguais ou muito parecidas."})
        if str(q.get("correta", "")).upper()[:1] not in ["A", "B", "C", "D", "E"]:
            diagnosticos.append({"questao": numero, "tipo": "Gabarito inválido", "mensagem": "A alternativa correta precisa ser A, B, C, D ou E."})
        if not str(q.get("explicacao", "")).strip():
            diagnosticos.append({"questao": numero, "tipo": "Sem explicação", "mensagem": "Adicionar explicação ajuda na devolutiva e relatório."})
        if not isinstance(q.get("elemento_visual"), dict) or not q.get("elemento_visual"):
            diagnosticos.append({"questao": numero, "tipo": "Sem elemento visual", "mensagem": "Questões mais completas podem usar tabela, gráfico, esquema ou diagrama."})
        if not isinstance(q.get("resolucao"), list) or not q.get("resolucao"):
            diagnosticos.append({"questao": numero, "tipo": "Sem resolução", "mensagem": "O gabarito do professor fica melhor com resolução passo a passo."})
        if not isinstance(q.get("dados_calculo"), dict) or not q.get("dados_calculo"):
            diagnosticos.append({"questao": numero, "tipo": "Sem dados de cálculo", "mensagem": "Quando fizer sentido, adicione fórmula, valores e unidade para questão calculável."})
    return diagnosticos


@app.route("/avaliacao/<int:avaliacao_id>/melhorar", methods=["GET", "POST"])
def melhorar_avaliacao(avaliacao_id):
    avaliacao = carregar_avaliacao(avaliacao_id)
    if not avaliacao:
        flash("Avaliação não encontrada.", "warning")
        return redirect(url_for("home"))
    dados = montar_dados_avaliacao(avaliacao)
    if request.method == "POST":
        questoes = []
        gabarito = {}
        for q in dados["questoes"]:
            item = dict(q)
            numero = int(item.get("numero") or len(questoes) + 1)
            fallback_rico = _criar_questao_fallback(numero, dados.get("titulo", "Avaliação"), dados.get("materias", "Conhecimentos gerais"), dados.get("temas", ""))
            item["enunciado"] = re.sub(r"\s+", " ", str(item.get("enunciado", "")).strip())
            item["contexto"] = re.sub(r"\s+", " ", str(item.get("contexto", "")).strip())
            if len(item["contexto"]) < 35:
                item["contexto"] = fallback_rico.get("contexto", item["contexto"])
            if len(item["enunciado"]) < 25:
                item["enunciado"] = fallback_rico.get("enunciado", item["enunciado"])
            if not isinstance(item.get("elemento_visual"), dict) or not item.get("elemento_visual"):
                item["elemento_visual"] = fallback_rico.get("elemento_visual", {})
            if not isinstance(item.get("dados_calculo"), dict) or not item.get("dados_calculo"):
                item["dados_calculo"] = fallback_rico.get("dados_calculo", {})
            if not isinstance(item.get("resolucao"), list) or not item.get("resolucao"):
                item["resolucao"] = fallback_rico.get("resolucao", [])
            item["tipo_questao"] = item.get("tipo_questao") or fallback_rico.get("tipo_questao", "mista")
            item["dificuldade"] = item.get("dificuldade") or fallback_rico.get("dificuldade", "medio")
            item["competencia"] = item.get("competencia") or fallback_rico.get("competencia", "")
            item["explicacao"] = str(item.get("explicacao") or fallback_rico.get("explicacao") or f"A alternativa {item.get('correta', 'A')} é a resposta indicada no gabarito oficial.").strip()
            alts = item.get("alternativas", {}) if isinstance(item.get("alternativas"), dict) else {}
            item["alternativas"] = {a: re.sub(r"\s+", " ", str(alts.get(a, "")).strip()) or fallback_rico.get("alternativas", {}).get(a, "") for a in ["A", "B", "C", "D", "E"]}
            correta = str(item.get("correta", "A")).upper()[:1]
            if correta not in ["A", "B", "C", "D", "E"]:
                correta = "A"
            item["correta"] = correta
            questoes.append(item)
            gabarito[str(item.get("numero"))] = correta
        conn = conectar()
        try:
            conn.execute(
                """
                UPDATE avaliacoes
                SET questoes_json = ?, gabarito_json = ?, status_revisao = 'melhorada', atualizado_em = ?
                WHERE id = ? AND professor_id = ?
                """,
                (json.dumps(questoes, ensure_ascii=False), json.dumps(gabarito, ensure_ascii=False), agora_str(), int(avaliacao_id), int(professor_atual_id())),
            )
            conn.commit()
        finally:
            conn.close()
        flash("Melhorias aplicadas: textos normalizados, questões enriquecidas com visual/cálculo quando faltava, resoluções preenchidas e gabarito validado.", "success")
        return redirect(url_for("ver_avaliacao", avaliacao_id=avaliacao_id))
    diagnosticos = diagnosticar_avaliacao(dados)
    return render_template("melhorar_avaliacao.html", avaliacao=dados, diagnosticos=diagnosticos)


@app.route("/avaliacao/<int:avaliacao_id>/exportar-diario.csv")
def exportar_diario_classe(avaliacao_id):
    rel = calcular_relatorio_avaliacao(avaliacao_id)
    if not rel:
        flash("Avaliação não encontrada.", "warning")
        return redirect(url_for("home"))
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Matricula", "Nome", "Turma", "Nota Final", "Status"])
    for item in rel["alunos_resultados"]:
        linha = item["linha"]
        writer.writerow([linha["matricula"], linha["aluno_nome"], linha["turma"], str(linha["nota_percentual"]).replace(".", ","), "Corrigido"])
    resp = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f"attachment; filename=diario_avaliacao_{avaliacao_id}.csv"
    return resp


@app.route("/corrigir", methods=["POST"])
def corrigir():
    """Corrige folhas por upload sem usar fallback perigoso.

    QR não lido/inválido vira pendência de identificação assistida.
    O Atlas só salva nota quando sabe exatamente qual folha/prova/aluno é.
    """
    try:
        prova_base_id = int(request.form.get("prova_id") or 0)
    except Exception:
        prova_base_id = 0
    arquivos = request.files.getlist("folhas")

    if not arquivos:
        flash("Envie pelo menos uma folha.", "danger")
        return redirect(url_for("home"))

    conn = conectar()
    prova_base = buscar_prova_com_aluno(conn, prova_base_id) if prova_base_id else None
    if not prova_base:
        conn.close()
        flash("Selecione uma prova base válida. Ela será usada apenas para sugerir turma na identificação assistida.", "danger")
        return redirect(url_for("home"))

    total_corrigidas = 0
    pendentes = 0
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
            if not dados_qr or not dados_qr.get("prova_id"):
                registrar_identificacao_pendente(conn, caminho, origem="upload", prova_base_id=prova_base_id, mensagem=f"QR não lido em {arquivo.filename}.", dados_qr=dados_qr)
                pendentes += 1
                continue

            prova_corrigir = buscar_prova_com_aluno(conn, int(dados_qr["prova_id"]))
            ok_qr, msg_qr = validar_qr_folha(dados_qr, prova_corrigir)
            if not ok_qr:
                registrar_identificacao_pendente(conn, caminho, origem="upload", prova_base_id=prova_base_id, mensagem=f"{msg_qr} Arquivo: {arquivo.filename}.", dados_qr=dados_qr)
                pendentes += 1
                continue

            gabarito_oficial = json.loads(prova_corrigir["gabarito_json"])
            try:
                resultado = corrigir_imagem_web(
                    caminho_gabarito_aluno=str(caminho),
                    gabarito_oficial=gabarito_oficial,
                    usar_ia="auto",
                )
            except Exception as erro:
                avisos.append(f"Erro ao corrigir {arquivo.filename}: {erro}")
                continue

            resultado["dados_qr"] = dados_qr
            resultado.setdefault("processamento", {})["validacao_qr"] = msg_qr
            if "mapa_questoes_json" in prova_corrigir.keys():
                resultado["mapa_questoes"] = carregar_json_seguro(prova_corrigir["mapa_questoes_json"], {})
            nota = float(resultado["resultado"]["nota_percentual"])
            status_conf = "confiavel" if resultado["resultado"].get("anuladas_ou_em_branco", 0) == 0 else "revisao"

            conn.execute(
                """
                INSERT INTO resultados (professor_id, aluno_id, prova_id, nota_percentual, resultado_json, status_confianca, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(professor_atual_id()), int(prova_corrigir["aluno_id"]), int(prova_corrigir["id"]),
                    nota, json.dumps(resultado, ensure_ascii=False), status_conf, agora_str(),
                ),
            )
            total_corrigidas += 1

    conn.commit()
    conn.close()

    if total_corrigidas:
        flash(f"Correção finalizada. {total_corrigidas} folha(s) corrigida(s).", "success")
    if pendentes:
        flash(f"{pendentes} folha(s) ficaram aguardando identificação assistida porque o QR falhou ou estava inválido.", "warning")
    if not total_corrigidas and not pendentes:
        flash("Nenhuma folha válida foi enviada.", "warning")
    for aviso in avisos[:5]:
        flash(aviso, "warning")
    if len(avisos) > 5:
        flash(f"Mais {len(avisos) - 5} aviso(s) ocultado(s).", "warning")

    if pendentes and not total_corrigidas:
        return redirect(url_for("identificacoes_pendentes"))
    return redirect(url_for("home"))




if __name__ == "__main__":
    iniciar_worker_fila()
    porta = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=porta, debug=False, use_reloader=False)
