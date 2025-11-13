from girder_worker import GirderWorkerPluginABC


class SIVACORWorkerPlugin(GirderWorkerPluginABC):
    def __init__(self, app, *args, **kwargs):
        self.app = app

    def task_imports(self):
        return [
            "girder_sivacor.worker_plugin.run_submission",
        ]
