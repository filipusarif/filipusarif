import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME'] 
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}

def daily_readme(birthday):
    """
    Calculates the exact age based on the provided birthday.

    Args:
        birthday (datetime): The user's birth date.

    Returns:
        str: Formatted string of the user's age (e.g., 'XX years, XX months, XX days').
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')

def format_plural(unit):
    """
    Returns an 's' if the unit is plural to properly format time strings.

    Args:
        unit (int): The number of time units (years, months, or days).

    Returns:
        str: 's' if unit is not 1, otherwise an empty string.
    """
    return 's' if unit != 1 else ''

def simple_request(func_name, query, variables):
    """
    Executes a GraphQL POST request to the GitHub API with retry logic for timeouts and limits.

    Args:
        func_name (str): The name of the function calling this request (for error logging).
        query (str): The GraphQL query string.
        variables (dict): The variables to pass alongside the GraphQL query.

    Returns:
        requests.Response: The successful response object from the API.

    Raises:
        Exception: If the request fails after maximum retries.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS, timeout=20)
            if request.status_code == 200:
                return request
            elif request.status_code == 502:
                print(f"502 Bad Gateway at {func_name}. Retrying {attempt+1}/{max_retries}...")
                time.sleep(3)
            elif request.status_code == 403:
                print(f"403 API Limit at {func_name}. Waiting 5 seconds...")
                time.sleep(5)
            else:
                break 
        except requests.exceptions.RequestException as e:
            print(f"Connection dropped at {func_name}. Retrying {attempt+1}/{max_retries}...")
            time.sleep(4)
            
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)

def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Fetches the total repository count or total star count using GitHub's GraphQL API.

    Args:
        count_type (str): Either 'repos' to count repositories or 'stars' to count stargazers.
        owner_affiliation (list): Repository affiliations to include (e.g., ['OWNER']).
        cursor (str, optional): The pagination cursor. Defaults to None.
        add_loc (int, optional): Legacy parameter, defaults to 0.
        del_loc (int, optional): Legacy parameter, defaults to 0.

    Returns:
        int: The total count of repositories or stars.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])

def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Recursively fetches commits for a specific repository to calculate Lines of Code (LoC).

    Args:
        owner (str): The owner of the repository.
        repo_name (str): The name of the repository.
        data (list): Cache data lines.
        cache_comment (list): The comment block at the top of the cache file.
        addition_total (int, optional): Accumulated lines added. Defaults to 0.
        deletion_total (int, optional): Accumulated lines deleted. Defaults to 0.
        my_commits (int, optional): Accumulated commits by the user. Defaults to 0.
        cursor (str, optional): Pagination cursor. Defaults to None.

    Returns:
        tuple or int: A tuple of (addition_total, deletion_total, my_commits) if successful, or 0 if empty.

    Raises:
        Exception: If the GraphQL request hits an anti-abuse limit or repeatedly fails.
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS, timeout=20)
            
            if request.status_code == 200:
                if request.json()['data']['repository']['defaultBranchRef'] != None:
                    return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
                else: 
                    return 0
            elif request.status_code == 502:
                print(f"502 Bad Gateway at {repo_name}. Retrying {attempt+1}/{max_retries}...")
                time.sleep(4)
            elif request.status_code == 403:
                print(f"403 Rate Limit at {repo_name}. Waiting 10 seconds...")
                time.sleep(10)
            else:
                break
        except requests.exceptions.RequestException as e:
            print(f"Connection dropped while fetching data for {repo_name}. Retrying {attempt+1}/{max_retries}...")
            time.sleep(5)
            
    force_close_file(data, cache_comment) 
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)

def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Parses the commit history of a single repository to sum up lines of code changes.

    Args:
        owner (str): Repository owner.
        repo_name (str): Repository name.
        data (list): Cache file data lines.
        cache_comment (list): Cache file comment block.
        history (dict): The history edges returned by the GraphQL query.
        addition_total (int): Current sum of added lines.
        deletion_total (int): Current sum of deleted lines.
        my_commits (int): Current sum of user's commits.

    Returns:
        tuple: (addition_total, deletion_total, my_commits) accumulated values.
    """
    for node in history['edges']:
        if node['node']['author']['user'] is not None and node['node']['author']['user']['id'] == OWNER_ID['id']:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])

def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Queries all repositories the user has access to, to prepare for LoC calculation.

    Args:
        owner_affiliation (list): Affiliations to filter repos (e.g., ['OWNER']).
        comment_size (int, optional): Size of the comment block in the cache file. Defaults to 0.
        force_cache (bool, optional): If True, forces a complete cache rebuild. Defaults to False.
        cursor (str, optional): Pagination cursor. Defaults to None.
        edges (list, optional): Accumulated repository edges. Defaults to [].

    Returns:
        list: Returns the output of cache_builder containing [loc_add, loc_del, total_loc, cached_status].
    """
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:
        edges += request.json()['data']['user']['repositories']['edges']
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)

def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository against a local cache file to see if updates are needed to calculate LoC.

    Args:
        edges (list): A list of repository nodes from the GraphQL query.
        comment_size (int): The number of lines occupied by the comment block in the cache file.
        force_cache (bool): Whether to force flush and rebuild the cache.
        loc_add (int, optional): Total lines added. Defaults to 0.
        loc_del (int, optional): Total lines deleted. Defaults to 0.

    Returns:
        list: [total_additions, total_deletions, net_additions, boolean_is_cached].
    """
    cached = True 
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' 
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: 
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: 
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError:
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]

def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file to rebuild it from scratch.

    Args:
        edges (list): Repository data to be cached.
        filename (str): Path to the cache file.
        comment_size (int): Size of the header comment block.
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] 
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')

def force_close_file(data, cache_comment):
    """
    Forces the cache file to save partially calculated data in case of a crash or rate limit.

    Args:
        data (list): The list of strings containing repo cache states.
        cache_comment (list): The comment block strings.
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('Error writing to cache file. Saved partial data.')

def stars_counter(data):
    """
    Counts the total number of stars across all repositories.

    Args:
        data (list): List of repository nodes containing stargazers data.

    Returns:
        int: Total star count.
    """
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars

def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parses an SVG file and overwrites specific text elements with real-time GitHub data.

    Args:
        filename (str): The path to the SVG file.
        age_data (str): Formatted age string.
        commit_data (int): Total commits.
        star_data (int): Total stars.
        repo_data (int): Total owned repositories.
        contrib_data (int): Total contributed repositories.
        follower_data (int): Total followers.
        loc_data (list): A list containing [lines_added, lines_deleted, total_net_lines].
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)

def justify_format(root, element_id, new_text, length=0):
    """
    Updates the text of an SVG element and manipulates a secondary 'dots' element to justify text alignment.

    Args:
        root (lxml.etree.Element): The root of the XML tree.
        element_id (str): The target SVG element ID.
        new_text (int or str): The new data value to insert.
        length (int, optional): The expected length of the string for justification. Defaults to 0.
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)

def find_and_replace(root, element_id, new_text):
    """
    Finds a specific element by ID using XPath and replaces its text content.

    Args:
        root (lxml.etree.Element): The root of the XML tree.
        element_id (str): The ID of the element to modify.
        new_text (str): The text to replace the existing content.
    """
    elements = root.xpath(f"//*[@id='{element_id}']")
    if elements:
        elements[0].text = str(new_text)
    else:
        print(f"Warning: Element with id '{element_id}' not found in the SVG.")

def commit_counter(comment_size):
    """
    Tally up the total commits from the saved local cache file.

    Args:
        comment_size (int): The number of lines to skip (comment block) in the cache file.

    Returns:
        int: The total commit count.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'r') as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits

def user_getter(username):
    """
    Fetches the account ID and creation date of a specific GitHub user.

    Args:
        username (str): The GitHub username.

    Returns:
        tuple: ({'id': str}, str) containing the user's ID dictionary and creation timestamp.
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Fetches the exact follower count for the specified user.

    Args:
        username (str): The GitHub username.

    Returns:
        int: Total number of followers.
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])

def query_count(funct_id):
    """
    Tracks how many times specific GitHub GraphQL API endpoints are called.

    Args:
        funct_id (str): The identifier name of the function making the query.
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1

def perf_counter(funct, *args):
    """
    Calculates the execution time of a specific function.

    Args:
        funct (callable): The function to measure.
        *args: Variable length argument list to pass into the target function.

    Returns:
        tuple: (Function Result, Execution Time Differential in seconds).
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start

def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Formats and prints the performance metrics to the console.

    Args:
        query_type (str): The label for the process being logged.
        difference (float): The time differential calculated by perf_counter.
        funct_return (any, optional): The returned data from the process. Defaults to False.
        whitespace (int, optional): Spacing format padding. Defaults to 0.

    Returns:
        any: The passed funct_return data.
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return

if __name__ == '__main__':
    print('Calculation times:')
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2000, 1, 1))
    formatter('age calculation', age_time)
    
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) 

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
        '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time)),
        ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))