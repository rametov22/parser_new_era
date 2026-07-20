class ScraperRouter:
    def db_for_read(self, model, **hints):
        if not model._meta.managed:
            return "main_db"
        return "default"

    def db_for_write(self, model, **hints):
        if not model._meta.managed:
            return "main_db"
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        # Разрешаем связи между объектами, если они оба из основной базы
        if not obj1._meta.managed or not obj2._meta.managed:
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Никогда не запускаем миграции для внешних таблиц
        if db == "main_db":
            return False
        return True
