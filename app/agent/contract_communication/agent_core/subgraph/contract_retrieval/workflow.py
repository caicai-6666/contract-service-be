"""先按业务定义对齐，再执行多轮检索；拒绝或技术失败均直接结束。"""
from functools import partial
from langgraph.graph import START, END, StateGraph
from app.agent.contract_extraction.subgraph.field_extraction.catalog import load_field_definition_catalog
from app.agent.contract_extraction.subgraph.classification.catalog import load_contract_category_catalog
from .state import ContractRetrievalInput, ContractRetrievalOutput, ContractRetrievalState
from .node import align_contract_query, retrieve_contracts


def build_contract_retrieval_subgraph(*, field_catalog=None, category_catalog=None, alignment_client=None, **options):
    settings = options['settings']
    # 正式服务注入启动快照；独立调用显式加载同一配置，不创建另一份字段契约。
    field_catalog = field_catalog or load_field_definition_catalog(settings.field_definition_path)
    category_catalog = category_catalog or load_contract_category_catalog(settings.contract_category_definition_path)
    graph = StateGraph(ContractRetrievalState,input_schema=ContractRetrievalInput,output_schema=ContractRetrievalOutput)
    graph.add_node('align_query',partial(align_contract_query,settings=settings,authorize=options['authorize'],
        field_catalog=field_catalog,category_catalog=category_catalog,
        client=alignment_client if alignment_client is not None else options.get('client'),audit=options.get('audit')))
    graph.add_node('retrieve_contracts',partial(retrieve_contracts,field_catalog=field_catalog,category_catalog=category_catalog,**options))
    graph.add_edge(START,'align_query')
    graph.add_conditional_edges('align_query',lambda state:'retrieve_contracts' if state['alignment_passed'] else END)
    graph.add_edge('retrieve_contracts',END)
    return graph.compile()
