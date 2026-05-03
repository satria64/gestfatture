from flask_login import LoginManager
from models import User

login_manager = LoginManager()
login_manager.login_view        = "login"
login_manager.login_message     = "Accedi per continuare."
login_manager.login_message_category = "warning"

@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))
