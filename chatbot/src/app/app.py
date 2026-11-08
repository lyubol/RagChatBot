"""PDF RAG chatbot — a Databricks App (Streamlit).

Answers questions about the PDFs in the `docs` volume by retrieving the most
similar chunks from the Vector Search index (built by the pdf_rag_ingestion
job) and generating a grounded answer with a Unity Gateway model service.
Every answer is backed by citations to the source PDF and chunk.

Authentication is fully managed by the Databricks Apps runtime: WorkspaceClient
picks up the app service principal's credentials from the environment, so no
tokens are handled in code.
"""

import os

import requests
import streamlit as st
from databricks.sdk import WorkspaceClient

# --- Configuration (from app.yaml env, with sane defaults) ---------------
VS_ENDPOINT_NAME = os.environ.get("VS_ENDPOINT_NAME", "rag-chatbot-dev")
VS_INDEX_NAME = os.environ.get("VS_INDEX_NAME", "rag_chatbot_dev.silver.doc_chunks_builtin_vs")
LLM_MODEL = os.environ.get("LLM_MODEL", "system.ai.meta-llama-3-3-70b-instruct")
NUM_CHUNKS = int(os.environ.get("NUM_CHUNKS", "4"))

SYSTEM_PROMPT = (
    "You are an assistant that answers questions about a small corpus of company "
    "PDF documents. Answer ONLY from the numbered context chunks provided. If the "
    "context does not contain the answer, say you don't know — do not invent "
    "anything. Cite the chunks you used inline, e.g. [1], [2]. Keep answers "
    "concise and factual."
)


@st.cache_resource
def get_workspace_client() -> WorkspaceClient:
    """One client per app process (Apps runtime injects its credentials)."""
    return WorkspaceClient()


w = get_workspace_client()


# --- RAG steps ------------------------------------------------------------
def retrieve(question: str) -> list[dict]:
    """Semantic search over the chunk index; returns the top chunks."""
    res = w.vector_search_indexes.query_index(
        index_name=VS_INDEX_NAME,
        columns=["doc_name", "chunk_num", "content"],
        query_text=question,
        num_results=NUM_CHUNKS,
    )
    # Each row: [doc_name, chunk_num, content, score] (score appended last)
    return [
        {"doc_name": row[0], "chunk_num": row[1], "content": row[2], "score": row[-1]}
        for row in res.result.data_array
    ]


def generate(question: str, chunks: list[dict]) -> str:
    """Grounded answer via Unity Gateway (MLflow Chat Completions)."""
    context = "\n\n".join(
        f"[{i + 1}] {c['doc_name']} (chunk {c['chunk_num']}):\n{c['content']}"
        for i, c in enumerate(chunks)
    )
    response = requests.post(
        f"{w.config.host.rstrip('/')}/ai-gateway/mlflow/v1/chat/completions",
        headers={**w.config.authenticate(), "Content-Type": "application/json"},
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Context:\n\n{context}\n\nQuestion: {question}"},
            ],
            "max_tokens": 1024,
            "temperature": 0.1,
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


# --- Chat UI ----------------------------------------------------------------
st.set_page_config(page_title="PDF Chatbot", page_icon="\U0001F4C4")
st.title("\U0001F4C4 PDF Chatbot")
st.caption("Ask anything about the indexed company documents — every answer cites its sources.")

if "messages" not in st.session_state:
    st.session_state.messages = []


def sources_expander(sources: list[dict]) -> None:
    """Render the citations for one assistant answer."""
    with st.expander("\U0001F4DA Sources used"):
        for i, s in enumerate(sources, start=1):
            st.markdown(
                f"**[{i}]** {s['doc_name']} — chunk {s['chunk_num']} "
                f"(similarity {s['score']:.3f})"
            )


def render(msg: dict) -> None:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("sources"):
            sources_expander(msg["sources"])


for msg in st.session_state.messages:
    render(msg)

question = st.chat_input("e.g. How many days of annual leave do I get?")
if question:
    st.session_state.messages.append({"role": "user", "content": question})
    render(st.session_state.messages[-1])

    chunks: list[dict] = []
    with st.chat_message("assistant"):
        try:
            with st.spinner("Searching the documents..."):
                chunks = retrieve(question)
            if not chunks:
                answer = "I couldn't find anything relevant in the indexed documents."
            else:
                with st.spinner("Reading the retrieved chunks..."):
                    answer = generate(question, chunks)
                sources_expander(chunks)
            st.markdown(answer)
        except Exception as e:  # noqa: BLE001 — surface any failure to the user
            answer = f"Sorry — something went wrong: `{e}`"
            st.error(answer)
    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "sources": chunks or None}
    )