# Schenkel Startup Search

Schenkel Startup Search e uma ferramenta para centralizar a busca de vagas na area de dados em um so lugar.

A ideia aqui foi unir tres fontes diferentes que ja existiam no projeto, cada uma com suas particularidades, e transformar tudo em uma interface unica em Streamlit, mais simples de usar no dia a dia.

Aplicacao publicada:
[https://startupjobsradar.streamlit.app/](https://startupjobsradar.streamlit.app/)

## O que a ferramenta faz

- Busca vagas em fontes como Greenhouse, Gupy e InHire
- Aplica filtros por termos de inclusao e exclusao
- Mostra a origem de cada vaga
- Indica quando a vaga e remota, quando essa informacao existe
- Atualiza os resultados aos poucos, conforme cada fonte termina de carregar
- Permite baixar os resultados em CSV e Excel

## Sobre as fontes

Cada plataforma funciona de um jeito diferente:

- Greenhouse: busca por boards selecionados
- Gupy: busca pela API publica com base nos termos informados
- InHire: usa um scraper dedicado, porque a estrutura dessa fonte exige mais cuidado

No caso do InHire, a informacao de remoto nem sempre esta disponivel na listagem. Quando isso acontece, a ferramenta mostra `N/A` em vez de esconder a vaga.

## Como usar

1. Escolha as fontes que quer consultar
2. Ajuste os termos de inclusao e exclusao
3. Se quiser, filtre por vagas remotas
4. Clique em `Buscar vagas agora`
5. Va abrindo e se candidatando nas vagas enquanto as outras fontes continuam carregando

## Rodando localmente

Clone o repositorio e instale as dependencias:

```bash
pip install -r requirements.txt
```

Depois rode:

```bash
streamlit run streamlit_app.py
```

## Observacao sobre o InHire

O InHire depende de Playwright para funcionar bem.

Se estiver rodando localmente e ele nao abrir, rode tambem:

```bash
python -m playwright install chromium
```

## Estrutura principal

- `streamlit_app.py`: ponto de entrada para o deploy
- `buscador_unificado.py`: logica principal da aplicacao
- `requirements.txt`: dependencias Python
- `packages.txt`: dependencias de sistema para deploy
- `Dockerfile`: imagem pronta para deploy no Render
- `render.yaml`: configuracao da web service no Render

## Deploy no Render

Para o caso do InHire, o Render e uma opcao melhor que o Streamlit Community Cloud porque permite subir a aplicacao com Docker e com a base do Playwright ja preparada.

Arquivos preparados para isso:

- `Dockerfile`
- `render.yaml`
- `streamlit_app.py`

No Render, o deploy recomendado e como `Web Service` com runtime `Docker`.

## Objetivo do projeto

O foco nao e apenas juntar vagas, mas reduzir o atrito da busca.

Em vez de abrir varias ferramentas separadas, a proposta e ter uma visao unica, mais clara, com contexto suficiente para decidir rapido o que vale aplicar.
