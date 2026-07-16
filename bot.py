"""Точка входа. Весь код живёт в пакете omanko/.

Файл оставлен в корне намеренно: CMD в Dockerfile ссылается на него,
и Railway не должен ничего заметить от распила на модули.
"""
from omanko.app import main

if __name__ == "__main__":
    main()
