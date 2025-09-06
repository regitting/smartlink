from app import create_app
from app.models import db, Link, Click

app = create_app()
with app.app_context():
    link = Link.query.filter_by(slug="resume").first()
    if link:
        Click.query.filter_by(link_id=link.id).delete()
        db.session.commit()
        print(f"Cleared old metrics for slug={link.slug}")
    else:
        print("No link with slug=resume found")