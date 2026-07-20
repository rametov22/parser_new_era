class ScraperRouter:
    MAIN_DB_MANAGED_MODELS = {"scrapers.veoveocontent"}

    @classmethod
    def _uses_main_db(cls, model):
        return (
            not model._meta.managed
            or model._meta.label_lower in cls.MAIN_DB_MANAGED_MODELS
        )

    def db_for_read(self, model, **hints):
        if self._uses_main_db(model):
            return "main_db"
        return "default"

    def db_for_write(self, model, **hints):
        if self._uses_main_db(model):
            return "main_db"
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        if self._uses_main_db(obj1) and self._uses_main_db(obj2):
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        model_label = f"{app_label}.{model_name}" if model_name else ""
        if model_label in self.MAIN_DB_MANAGED_MODELS:
            return db == "main_db"
        if db == "main_db":
            return False
        return True
