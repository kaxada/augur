#SPDX-License-Identifier: MIT

import sys
import time
import traceback
import logging
import platform
import imp
import time
import datetime
import html.parser
import subprocess
import os
import getopt
import xlsxwriter
import configparser
import multiprocessing
import numpy as np
from celery import group, chain, chord, signature
from celery.utils.log import get_task_logger
from celery.result import allow_join_result
from celery.signals import after_setup_logger
from datetime import timedelta
import sqlalchemy as s

from sqlalchemy import or_, and_, update

from augur.tasks.git.util.facade_worker.facade_worker.utilitymethods import update_repo_log, trim_commit, store_working_author, trim_author
from augur.tasks.git.util.facade_worker.facade_worker.utilitymethods import get_absolute_repo_path, get_parent_commits_set, get_existing_commits_set
from augur.tasks.git.util.facade_worker.facade_worker.analyzecommit import analyze_commit
from augur.tasks.git.util.facade_worker.facade_worker.utilitymethods import get_facade_weight_time_factor, get_repo_commit_count, update_facade_scheduling_fields, get_facade_weight_with_commit_count

from augur.tasks.github.facade_github.tasks import *
from augur.tasks.util.collection_util import CollectionState, get_collection_status_repo_git_from_filter
from augur.tasks.git.util.facade_worker.facade_worker.repofetch import GitCloneError, git_repo_initialize


from augur.tasks.util.worker_util import create_grouped_task_load

from augur.tasks.init.celery_app import celery_app as celery
from augur.tasks.init.celery_app import AugurFacadeRepoCollectionTask


from augur.tasks.util.AugurUUID import GithubUUID, UnresolvableUUID
from augur.application.db.models import PullRequest, Message, PullRequestReview, PullRequestLabel, PullRequestReviewer, PullRequestEvent, PullRequestMeta, PullRequestAssignee, PullRequestReviewMessageRef, Issue, IssueEvent, IssueLabel, IssueAssignee, PullRequestMessageRef, IssueMessageRef, Contributor, Repo, CollectionStatus

from augur.tasks.git.dependency_tasks.tasks import process_dependency_metrics
from augur.tasks.git.dependency_libyear_tasks.tasks import process_libyear_dependency_metrics
from augur.tasks.git.scc_value_tasks.tasks import process_scc_value_metrics

from augur.tasks.github.util.github_paginator import GithubPaginator, hit_api
from augur.tasks.github.util.gh_graphql_entities import PullRequest
from augur.tasks.github.util.github_task_session import *

from augur.application.logs import TaskLogConfig

#define an error callback for chains in facade collection so facade doesn't make the program crash
#if it does.
@celery.task
def facade_error_handler(request,exc,traceback):

    logger = logging.getLogger(facade_error_handler.__name__)

    logger.error(f"Task {request.id} raised exception: {exc}! \n {traceback}")

    print(f"chain: {request.chain}")
    #Make sure any further execution of tasks dependent on this one stops.
    try:
        #Replace the tasks queued ahead of this one in a chain with None.
        request.chain = None
    except AttributeError:
        pass #Task is not part of a chain. Normal so don't log.
    except Exception as e:
        logger.error(f"Could not mutate request chain! \n Error: {e}")


#Predefine facade collection with tasks
@celery.task(base=AugurFacadeRepoCollectionTask)
def facade_analysis_init_facade_task(repo_git):

    logger = logging.getLogger(facade_analysis_init_facade_task.__name__)
    with FacadeSession(logger) as session:

        repo = session.query(Repo).filter(Repo.repo_git == repo_git).one()
        repo_id = repo.repo_id

        session.update_status('Running analysis')
        session.log_activity('Info', "Beginning analysis.")


@celery.task(base=AugurFacadeRepoCollectionTask)
def trim_commits_facade_task(repo_git):

    logger = logging.getLogger(trim_commits_facade_task.__name__)

    with FacadeSession(logger) as session:

        repo = session.query(Repo).filter(Repo.repo_git == repo_git).one()
        repo_id = repo.repo_id

        def update_analysis_log(repos_id,status):

        # Log a repo's analysis status

            log_message = s.sql.text("""INSERT INTO analysis_log (repos_id,status)
                VALUES (:repo_id,:status)""").bindparams(repo_id=repos_id,status=status)

            try:
                session.execute_sql(log_message)
            except:
                pass


        session.inc_repos_processed()
        update_analysis_log(repo_id,"Beginning analysis.")
        # First we check to see if the previous analysis didn't complete

        get_status = s.sql.text("""SELECT working_commit FROM working_commits WHERE repos_id=:repo_id
            """).bindparams(repo_id=repo_id)

        try:
            working_commits = session.fetchall_data_from_sql_text(get_status)
        except:
            working_commits = []

        # If there's a commit still there, the previous run was interrupted and
        # the commit data may be incomplete. It should be trimmed, just in case.
        for commit in working_commits:
            trim_commit(session, repo_id,commit['working_commit'])

            # Remove the working commit.
            remove_commit = s.sql.text("""DELETE FROM working_commits
                WHERE repos_id = :repo_id AND 
                working_commit = :commit""").bindparams(repo_id=repo_id,commit=commit['working_commit'])
            session.execute_sql(remove_commit)
            session.log_activity('Debug',f"Removed working commit: {commit['working_commit']}")

        # Start the main analysis

        update_analysis_log(repo_id,'Collecting data')
        logger.info(f"Got past repo {repo_id}")

@celery.task(base=AugurFacadeRepoCollectionTask)
def trim_commits_post_analysis_facade_task(repo_git):

    logger = logging.getLogger(trim_commits_post_analysis_facade_task.__name__)
    

    with FacadeSession(logger) as session:
        repo = session.query(Repo).filter(Repo.repo_git == repo_git).one()
        repo_id = repo.repo_id

        start_date = session.get_setting('start_date')
        def update_analysis_log(repos_id,status):

            # Log a repo's analysis status

            log_message = s.sql.text("""INSERT INTO analysis_log (repos_id,status)
                VALUES (:repo_id,:status)""").bindparams(repo_id=repos_id,status=status)

            
            session.execute_sql(log_message)
        
        session.logger.info(f"Generating sequence for repo {repo_id}")

        query = session.query(Repo).filter(Repo.repo_id == repo_id)
        repo = execute_session_query(query, 'one')

        #Get the huge list of commits to process.
        absoulte_path = get_absolute_repo_path(session.repo_base_directory, repo.repo_id, repo.repo_path,repo.repo_name)
        repo_loc = (f"{absoulte_path}/.git")
        # Grab the parents of HEAD

        parent_commits = get_parent_commits_set(repo_loc, start_date)

        # Grab the existing commits from the database
        existing_commits = get_existing_commits_set(session, repo_id)

        # Find missing commits and add them

        missing_commits = parent_commits - existing_commits

        session.log_activity('Debug',f"Commits missing from repo {repo_id}: {len(missing_commits)}")
        
        # Find commits which are out of the analysis range

        trimmed_commits = existing_commits - parent_commits

        update_analysis_log(repo_id,'Data collection complete')

        update_analysis_log(repo_id,'Beginning to trim commits')

        session.log_activity('Debug',f"Commits to be trimmed from repo {repo_id}: {len(trimmed_commits)}")



        for commit in trimmed_commits:
            trim_commit(session,repo_id,commit)
        

        update_analysis_log(repo_id,'Commit trimming complete')

        update_analysis_log(repo_id,'Complete')
    


@celery.task
def facade_analysis_end_facade_task():

    logger = logging.getLogger(facade_analysis_end_facade_task.__name__)
    with FacadeSession(logger) as session:
        session.log_activity('Info','Running analysis (complete)')



@celery.task
def facade_start_contrib_analysis_task():

    logger = logging.getLogger(facade_start_contrib_analysis_task.__name__)
    with FacadeSession(logger) as session:
        session.update_status('Updating Contributors')
        session.log_activity('Info', 'Updating Contributors with commits')


#enable celery multithreading
@celery.task(base=AugurFacadeRepoCollectionTask)
def analyze_commits_in_parallel(repo_git, multithreaded: bool) -> None:
    """Take a large list of commit data to analyze and store in the database. Meant to be run in parallel with other instances of this task.
    """

    #create new session for celery thread.
    logger = logging.getLogger(analyze_commits_in_parallel.__name__)
    with FacadeSession(logger) as session:
        
        repo = session.query(Repo).filter(Repo.repo_git == repo_git).one()
        repo_id = repo.repo_id

        start_date = session.get_setting('start_date')

        session.logger.info(f"Generating sequence for repo {repo_id}")

        query = session.query(Repo).filter(Repo.repo_id == repo_id)
        repo = execute_session_query(query, 'one')

        #Get the huge list of commits to process.
        absoulte_path = get_absolute_repo_path(session.repo_base_directory, repo.repo_id, repo.repo_path, repo.repo_name)
        repo_loc = (f"{absoulte_path}/.git")
        # Grab the parents of HEAD

        parent_commits = get_parent_commits_set(repo_loc, start_date)

        # Grab the existing commits from the database
        existing_commits = get_existing_commits_set(session, repo_id)

        # Find missing commits and add them
        missing_commits = parent_commits - existing_commits

        session.log_activity('Debug',f"Commits missing from repo {repo_id}: {len(missing_commits)}")

        if len(missing_commits) <= 0:
            return

        #session.log_activity('Info','Type of missing_commits: %s' % type(missing_commits))

        #encode the repo_id with the commit.
        commits = list(missing_commits)
        queue = list(commits)
        logger.info("Got to analysis!")

        for count, commitTuple in enumerate(queue):
            quarterQueue = len(queue) // 4

            if quarterQueue == 0:
                quarterQueue = 1 # prevent division by zero with integer math

            #Log progress when another quarter of the queue has been processed
            if (count + 1) % quarterQueue == 0:
                logger.info(f"Progress through current analysis queue is {(count / len(queue)) * 100}%")


            #logger.info(f"Got to analysis!")
            absoulte_path = get_absolute_repo_path(session.repo_base_directory, repo.repo_id, repo.repo_path,repo.repo_name)
            repo_loc = (f"{absoulte_path}/.git")


            analyze_commit(session, repo_id, repo_loc, commitTuple)

        logger.info("Analysis complete")
    return

@celery.task
def nuke_affiliations_facade_task():

    logger = logging.getLogger(nuke_affiliations_facade_task.__name__)
    
    with FacadeSession(logger) as session:
        nuke_affiliations(session)

@celery.task
def fill_empty_affiliations_facade_task():

    logger = logging.getLogger(fill_empty_affiliations_facade_task.__name__)
    with FacadeSession(logger) as session:
        fill_empty_affiliations(session)

@celery.task
def invalidate_caches_facade_task():

    logger = logging.getLogger(invalidate_caches_facade_task.__name__)

    with FacadeSession(logger) as session:
        invalidate_caches(session)

@celery.task
def rebuild_unknown_affiliation_and_web_caches_facade_task():

    logger = logging.getLogger(rebuild_unknown_affiliation_and_web_caches_facade_task.__name__)
    
    with FacadeSession(logger) as session:
        rebuild_unknown_affiliation_and_web_caches(session)


@celery.task
def git_repo_cleanup_facade_task(repo_git):

    logger = logging.getLogger(git_repo_cleanup_facade_task.__name__)

    with FacadeSession(logger) as session:
        git_repo_cleanup(session, repo_git)

# retry this task indefinitely every 5 minutes if it errors. Since the only way it gets scheduled is by itself, so if it stops running no more clones will happen till the instance is restarted
@celery.task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=300, retry_jitter=True, max_retries=None)
def clone_repos():

    logger = logging.getLogger(clone_repos.__name__)
    
    is_pending = CollectionStatus.facade_status == CollectionState.PENDING.value

    with FacadeSession(logger) as session:

        # process up to 1000 repos at a time
        repo_git_identifiers = get_collection_status_repo_git_from_filter(session, is_pending, 999999)
        for repo_git in repo_git_identifiers:
            # set repo to intializing
            repo = session.query(Repo).filter(Repo.repo_git == repo_git).one()
            repoStatus = repo.collection_status[0]
            setattr(repoStatus,"facade_status", CollectionState.INITIALIZING.value)
            session.commit()

            # clone repo
            try:
                git_repo_initialize(session, repo_git)
                session.commit()

                # get the commit count
                commit_count = get_repo_commit_count(session, repo_git)
                facade_weight = get_facade_weight_with_commit_count(session, repo_git, commit_count)

                update_facade_scheduling_fields(session, repo_git, facade_weight, commit_count)

                # set repo to update
                setattr(repoStatus,"facade_status", CollectionState.UPDATE.value)
                session.commit()
            except GitCloneError:
                # continue to next repo, since we can't calculate 
                # commit_count or weight without the repo cloned
                setattr(repoStatus,"facade_status", CollectionState.FAILED_CLONE.value)
                session.commit()
            except Exception as e:
                logger.error(f"Ran into unexpected issue when cloning repositories \n Error: {e}")
                # set repo to error
                setattr(repoStatus,"facade_status", CollectionState.ERROR.value)
                session.commit()

        clone_repos.si().apply_async(countdown=60*5)




#@celery.task
#def check_for_repo_updates_facade_task(repo_git):
#
#    from augur.tasks.init.celery_app import engine
#
#    logger = logging.getLogger(check_for_repo_updates_facade_task.__name__)
#
#    with FacadeSession(logger) as session:
#        check_for_repo_updates(session, repo_git)

@celery.task
def git_update_commit_count_weight(repo_git):

    from augur.tasks.init.celery_app import engine
    logger = logging.getLogger(git_update_commit_count_weight.__name__)
    
    with FacadeSession(logger) as session:
        commit_count = get_repo_commit_count(session, repo_git)
        facade_weight = get_facade_weight_with_commit_count(session, repo_git, commit_count)

        update_facade_scheduling_fields(session, repo_git, facade_weight, commit_count)


@celery.task
def git_repo_updates_facade_task(repo_git):

    logger = logging.getLogger(git_repo_updates_facade_task.__name__)

    with FacadeSession(logger) as session:
        git_repo_updates(session, repo_git)


def generate_analysis_sequence(logger,repo_git, session):
    """Run the analysis by looping over all active repos. For each repo, we retrieve
    the list of commits which lead to HEAD. If any are missing from the database,
    they are filled in. Then we check to see if any commits in the database are
    not in the list of parents, and prune them out.

    We also keep track of the last commit to be processed, so that if the analysis
    is interrupted (possibly leading to partial data in the database for the
    commit being analyzed at the time) we can recover.
    """

    
    
    repo_list = s.sql.text("""SELECT repo_id,repo_group_id,repo_path,repo_name FROM repo 
    WHERE repo_git=:value""").bindparams(value=repo_git)
    repos = session.fetchall_data_from_sql_text(repo_list)

    start_date = session.get_setting('start_date')

    repo_ids = [repo['repo_id'] for repo in repos]

    repo_id = repo_ids.pop(0)

    analysis_sequence = [
        facade_analysis_init_facade_task.si(repo_git),
        trim_commits_facade_task.si(repo_git),
        analyze_commits_in_parallel.si(repo_git, True),
        trim_commits_post_analysis_facade_task.si(repo_git),
        facade_analysis_end_facade_task.si(),
    ]
    logger.info(f"Analysis sequence: {analysis_sequence}")
    return analysis_sequence



def generate_contributor_sequence(logger,repo_git, session):
    
    contributor_sequence = []
    #all_repo_ids = []
    repo_id = None
        
    #contributor_sequence.append(facade_start_contrib_analysis_task.si())
    query = s.sql.text("""SELECT repo_id FROM repo
    WHERE repo_git=:value""").bindparams(value=repo_git)

    repo = session.execute_sql(query).fetchone()
    session.logger.info(f"repo: {repo}")
    repo_id = repo[0]
    #pdb.set_trace()
    #breakpoint()
    #for repo in all_repos:
    #    contributor_sequence.append(insert_facade_contributors.si(repo['repo_id']))
    #all_repo_ids = [repo['repo_id'] for repo in all_repos]

    #contrib_group = create_grouped_task_load(dataList=all_repo_ids,task=insert_facade_contributors)#group(contributor_sequence)
    #contrib_group.link_error(facade_error_handler.s())
    #return contrib_group#chain(facade_start_contrib_analysis_task.si(), contrib_group)
    return insert_facade_contributors.si(repo_id)


def facade_phase(repo_git):
    logger = logging.getLogger(facade_phase.__name__)
    logger.info("Generating facade sequence")
    with FacadeSession(logger) as session:
        #Get the repo_id
        repo_list = s.sql.text("""SELECT repo_id,repo_group_id,repo_path,repo_name FROM repo 
        WHERE repo_git=:value""").bindparams(value=repo_git)
        repos = session.fetchall_data_from_sql_text(repo_list)

        start_date = session.get_setting('start_date')

        repo_ids = [repo['repo_id'] for repo in repos]

        repo_id = repo_ids.pop(0)

        #Get the collectionStatus
        query = session.query(CollectionStatus).filter(CollectionStatus.repo_id == repo_id)

        status = execute_session_query(query,'one')

        # Figure out what we need to do
        limited_run = session.limited_run
        run_analysis = session.run_analysis
        pull_repos = session.pull_repos
        #force_analysis = session.force_analysis
        run_facade_contributors = session.run_facade_contributors

        facade_core_collection = []

        if not limited_run or pull_repos:
            facade_core_collection.append(git_repo_updates_facade_task.si(repo_git))

        facade_core_collection.append(git_update_commit_count_weight.si(repo_git))

        #Generate commit analysis task order.
        if not limited_run or run_analysis:
            facade_core_collection.extend(generate_analysis_sequence(logger,repo_git,session))

        #Generate contributor analysis task group.
        if not limited_run or run_facade_contributors:
            facade_core_collection.append(generate_contributor_sequence(logger,repo_git,session))


        facade_sequence = [
            group(
                chain(*facade_core_collection),
                process_dependency_metrics.si(repo_git),
                process_libyear_dependency_metrics.si(repo_git),
                process_scc_value_metrics.si(repo_git),
            )
        ]
        logger.info(f"Facade sequence: {facade_sequence}")
        return chain(*facade_sequence)

def generate_non_repo_domain_facade_tasks(logger):
    logger.info("Generating facade sequence")
    with FacadeSession(logger) as session:
        
        # Figure out what we need to do
        limited_run = session.limited_run
        delete_marked_repos = session.delete_marked_repos
        pull_repos = session.pull_repos
        # clone_repos = session.clone_repos
        check_updates = session.check_updates
        # force_updates = session.force_updates
        run_analysis = session.run_analysis
        # force_analysis = session.force_analysis
        nuke_stored_affiliations = session.nuke_stored_affiliations
        fix_affiliations = session.fix_affiliations
        force_invalidate_caches = session.force_invalidate_caches
        rebuild_caches = session.rebuild_caches
        force_invalidate_caches = session.force_invalidate_caches
        create_xlsx_summary_files = session.create_xlsx_summary_files
        multithreaded = session.multithreaded

        facade_sequence = []

        if nuke_stored_affiliations:
            #facade_sequence.append(nuke_affiliations_facade_task.si().on_error(facade_error_handler.s()))#nuke_affiliations(session.cfg)
            logger.info("Nuke stored affiliations is deprecated.")
            # deprecated because the UI component of facade where affiliations would be 
            # nuked upon change no longer exists, and this information can easily be derived 
            # from queries and materialized views in the current version of Augur.
            # This method is also a major performance bottleneck with little value.

        #session.logger.info(session.cfg)
        if not limited_run or fix_affiliations:
            #facade_sequence.append(fill_empty_affiliations_facade_task.si().on_error(facade_error_handler.s()))#fill_empty_affiliations(session)
            logger.info("Fill empty affiliations is deprecated.")
            # deprecated because the UI component of facade where affiliations would need 
            # to be fixed upon change no longer exists, and this information can easily be derived 
            # from queries and materialized views in the current version of Augur.
            # This method is also a major performance bottleneck with little value.

        if force_invalidate_caches:
            facade_sequence.append(invalidate_caches_facade_task.si().on_error(facade_error_handler.s()))#invalidate_caches(session.cfg)

        if not limited_run or rebuild_caches:
            facade_sequence.append(rebuild_unknown_affiliation_and_web_caches_facade_task.si().on_error(facade_error_handler.s()))#rebuild_unknown_affiliation_and_web_caches(session.cfg)

        return facade_sequence
