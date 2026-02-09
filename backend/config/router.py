class ScraperRouter:
    def db_for_read(self, model, **hints):
        if (
            model._meta.app_label == "scrapers"
            and model.__name__ == "ContentAppContent"
        ):
            return "main_db"
        return "default"

    def db_for_write(self, model, **hints):
        if (
            model._meta.app_label == "scrapers"
            and model.__name__ == "ContentAppContent"
        ):
            return "main_db"
        return "default"

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Запрещаем Django создавать свои системные таблицы в вашей основной базе
        if db == "main_db":
            return False
        return True
