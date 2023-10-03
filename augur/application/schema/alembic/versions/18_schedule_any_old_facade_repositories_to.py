"""Schedule any old facade repositories to be re-cloned

Revision ID: 18
Revises: 17
Create Date: 2023-05-02 17:35:18.891913

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text
import pathlib
import shutil
from augur.application.config import AugurConfig
from augur.application.db.session import DatabaseSession
import logging

# revision identifiers, used by Alembic.
revision = '18'
down_revision = '17'
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def total_facade_reset():
    conn = op.get_bind()

    conn.execute(
        text(
            """

    UPDATE augur_operations.collection_status
    SET facade_status='Pending', facade_task_id=NULL, facade_weight=NULL,commit_sum=NULL,facade_data_last_collected=NULL;

    UPDATE repo
    SET repo_path=NULL,repo_name=NULL;

    UPDATE augur_operations.collection_status
    SET core_status='Pending', secondary_status='Pending', core_data_last_collected=NULL,secondary_data_last_collected=NULL,core_task_id=NULL,secondary_task_id=NULL
    WHERE issue_pr_sum IS NULL;
    """
        )
    )

        

    try:
        with DatabaseSession(logger) as session:
            config = AugurConfig(logger, session)
            facade_base_dir = config.get_section("Facade")['repo_directory']

        #remove path
        path = pathlib.Path(facade_base_dir)

        #Move credentials out
        shutil.move(f"{facade_base_dir}.git-credentials","/tmp/.git-credentials")

        shutil.rmtree(path)
        #Create path
        path.mkdir()
        #Move credentials in
        shutil.move("/tmp/.git-credentials",f"{facade_base_dir}.git-credentials")



    except:
        pass

def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    
    # ### end Alembic commands ###
    total_facade_reset()



def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    total_facade_reset()
