from flask import Flask, render_template, request, jsonify
import os
import re
import time
import json
import requests
from flask_cors import CORS

app = Flask(__name__, static_folder='static', template_folder='static')
CORS(app)

GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

if not GROQ_API_KEY:
    raise ValueError('Configure a variável de ambiente GROQ_API_KEY.')

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

TIPOS_ANALISE = {
    "sentimento": {
        "label": "Análise de Sentimento",
        "labels_possiveis": ["Positivo", "Negativo", "Neutro", "Misto"],
    },
    "intencao": {
        "label": "Análise de Intenção",
        "labels_possiveis": ["Elogio", "Reclamação", "Sugestão", "Dúvida", "Spam"],
    },
    "toxicidade": {
        "label": "Análise de Toxicidade",
        "labels_possiveis": ["Seguro", "Levemente Ofensivo", "Ofensivo", "Muito Ofensivo"],
    },
}


class QuotaExceededException(Exception):
    pass


def _build_prompt(comentario, tipo_analise, idioma):
    info = TIPOS_ANALISE[tipo_analise]
    labels = ", ".join(f'"{l}"' for l in info["labels_possiveis"])

    return f"""Você é um especialista em análise de texto. Classifique o comentário abaixo realizando uma {info["label"]}.

Idioma do comentário: {idioma}
Tipo de análise: {info["label"]}
Categorias possíveis: {labels}

Comentário:
\"\"\"{comentario}\"\"\"

Responda APENAS com um JSON válido no seguinte formato (sem markdown, sem explicações extras):
{{
  "classificacao": "<uma das categorias possíveis>",
  "confianca": <número inteiro de 0 a 100>,
  "resumo": "<explicação breve de 1 a 2 frases do motivo da classificação>",
  "palavras_chave": ["<palavra ou expressão relevante>", "<palavra ou expressão relevante>"],
  "tom": "<descrição curta do tom geral: ex. irônico, agressivo, entusiasmado, formal...>"
}}"""


def _post_with_retry(url, payload, headers, max_retries=3):
    for attempt in range(max_retries):
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code == 429:
            if attempt < max_retries - 1:
                wait = 2 ** attempt * 5
                print(f"Rate limit atingido. Aguardando {wait}s...")
                time.sleep(wait)
                continue
            return response
        return response
    return response


def _extract_json(text):
    """Extract JSON from model response, handling markdown code blocks."""
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers
    match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if match:
        text = match.group(1).strip()
    return json.loads(text)


def classificar_comentario(comentario, tipo_analise, idioma):
    prompt = _build_prompt(comentario, tipo_analise, idioma)
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    last_error = None
    for model in GROQ_MODELS:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            }
            response = _post_with_retry(GROQ_API_URL, payload, headers)

            if response.status_code == 429:
                last_error = "quota_exceeded"
                print(f"Modelo Groq {model} com rate limit, tentando próximo...")
                continue

            if response.status_code == 404:
                print(f"Modelo Groq {model} não encontrado, tentando próximo...")
                continue

            if response.status_code != 200:
                raise Exception(f"Erro na API Groq ({model}): {response.status_code} - {response.text}")

            response_data = response.json()
            text_response = (
                response_data.get("choices", [{}])[0]
                .get("message", {})
                .get("content")
            )

            if not text_response:
                raise Exception("A resposta da API Groq não contém o conteúdo esperado.")

            parsed = _extract_json(text_response)
            parsed["model_used"] = model
            parsed["tipo_analise"] = tipo_analise
            parsed["tipo_label"] = TIPOS_ANALISE[tipo_analise]["label"]
            parsed["labels_possiveis"] = TIPOS_ANALISE[tipo_analise]["labels_possiveis"]
            parsed["raw_response"] = text_response
            parsed["prompt_gerado"] = prompt
            return parsed

        except json.JSONDecodeError as e:
            raise Exception(f"A resposta do modelo não era um JSON válido: {str(e)}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Erro de conexão com a API Groq: {str(e)}")

    if last_error == "quota_exceeded":
        raise QuotaExceededException(
            "Rate limit da API Groq atingido para todos os modelos. "
            "Aguarde alguns segundos e tente novamente, ou verifique seu plano em https://console.groq.com"
        )
    raise Exception("Não foi possível classificar com nenhum modelo Groq disponível.")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/classificar', methods=['POST'])
def classificar():
    try:
        data = request.get_json()

        if not data.get('comentario', '').strip():
            return jsonify({'error': 'O campo comentário é obrigatório'}), 400

        tipo_analise = data.get('tipo_analise', 'sentimento').lower()
        if tipo_analise not in TIPOS_ANALISE:
            return jsonify({'error': f'Tipo de análise inválido: {tipo_analise}'}), 400

        idioma = data.get('idioma', 'Português')

        resultado = classificar_comentario(
            data['comentario'].strip(),
            tipo_analise,
            idioma,
        )
        return jsonify(resultado)

    except QuotaExceededException as e:
        print(f"Quota esgotada: {e}")
        return jsonify({'error': str(e)}), 429
    except Exception as e:
        print(f"Erro ao classificar: {e}")
        return jsonify({'error': f'Erro ao processar a solicitação: {str(e)}'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=True)
