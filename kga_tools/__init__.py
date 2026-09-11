def classFactory(iface):
    from .kga_plugin import KgaToolsPlugin
    return KgaToolsPlugin(iface)
