"""stdio MCP server；标准输出由协议占用，禁止 print 日志。"""
from mcp.server.fastmcp import FastMCP
from app.integrations.providers import execute

mcp = FastMCP('hengban-readonly-tools')


@mcp.tool()
def weather(city: str = '', target_date: str = '', latitude: float | None = None, longitude: float | None = None) -> dict:
    """按坐标或城市查询天气；Open-Meteo七天，高德回退通常四天；标注实况报告时间和实际覆盖。"""
    return execute('weather', {'city': city, 'target_date': target_date,'latitude':latitude,'longitude':longitude})


@mcp.tool()
def venues(city: str, query: str = '运动场馆') -> dict:
    """查询场地候选；不查询实时预约余量、不预约、不付款。"""
    return execute('venues', {'city': city, 'query': query})


@mcp.tool()
def search(query: str) -> dict:
    """搜索公开信息，返回来源和摘要，禁止在 query 中包含健康档案。"""
    return execute('search', {'query': query})


@mcp.tool()
def ingredients(query: str = '') -> dict:
    """查询内置食材示例目录，无实时库存或营养处方。"""
    return execute('ingredients', {'query': query})


@mcp.tool()
def outing(city: str = '', place_name: str = '', start_place: str = '', latitude: float | None = None, longitude: float | None = None, categories: list[str] | None = None, radius_m: int = 2000) -> dict:
    """按已授权坐标查询天气、附近场地与真实步行路径，不执行预约。"""
    return execute('outing', {'city': city, 'place_name': place_name, 'start_place': start_place, 'latitude': latitude, 'longitude': longitude, 'categories': categories, 'radius_m': radius_m})


@mcp.tool()
def restaurants(city: str = '', start_place: str = '', latitude: float | None = None,
                longitude: float | None = None, categories: list[str] | None = None, radius_m: int = 2000) -> dict:
    """查询附近真实餐馆和步行路线；菜单、过敏原、营养与实时价格未知，不订餐。"""
    return execute('restaurants', {'city': city, 'start_place': start_place, 'latitude': latitude,
                                  'longitude': longitude, 'categories': categories, 'radius_m': radius_m})


if __name__ == '__main__':
    mcp.run(transport='stdio')
