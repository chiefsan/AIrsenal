#!/usr/bin/env python

"""
usage:
python fill_transfersuggestsions_table.py --weeks_ahead <num_weeks_ahead> --num_iterations <num_iterations>
output for each strategy tried is going to be a dict
{ "total_points": <float>,
"points_per_gw": {<gw>: <float>, ...},
"players_sold" : {<gw>: [], ...},
"players_bought" : {<gw>: [], ...}
}
"""

import os
import sys
import time


import json


from multiprocessing import Process, Queue
from tqdm import tqdm
import argparse

from ..framework.optimization_utils import *

OUTPUT_DIR = "/tmp/airsopt"

def count_increments(strategy_string, num_iterations, exhaustive_double_transfer):
    """
    how many steps for the progress bar for this strategy
    """
    total = 0
    for s in strategy_string:
        if s=="W":
            ## wildcard - needs num_iterations iterations
            total+= num_iterations
        elif s=="1":
            ## single transfer - 15 increments (replace each player in turn)
            total+= 15
        elif s=="2":
            if exhaustive_double_transfer:
                ## remove each pair of players - 15*7=105 combinations
                total += 105
            else:
                total += num_iterations
        elif s=="3":
            total += num_iterations
    ## return at least 1, to avoid ZeroDivisionError
    return max(total,1)

def process_strat(queue, pid, num_iterations, exhaustive_double_transfer, tag, baseline=None, updater=None, resetter=None, budget=None):
    """
    subprocess to go through a strategy and output a json file with
    the best players in, players out, and total score.
    """
    while True:
        strat = queue.get()
        if strat == "DONE":
            resetter(pid,strat)
            break
        sid = make_strategy_id(strat)
        ## reset this process' progress bar, and give it the string for the
        ## next strategy
        resetter(pid, sid)

        ## count how many incremements for this progress bar / strategy
        num_increments = count_increments(sid,
                                          num_iterations,
                                          exhaustive_double_transfer)
        increment = 100 / num_increments
#        if (not strategy_involves_N_or_more_transfers_in_gw(strat,3)) or exhaustive_double_transfer:
#            num_iter = 1
#        else:
        num_iter = num_iterations
        strat_output = apply_strategy(strat,
                                      exhaustive_double_transfer,
                                      tag,
                                      baseline,
                                      num_iter,
                                      (updater,
                                       increment,
                                       pid),
                                      budget)
        with open(
            os.path.join(OUTPUT_DIR, "strategy_{}_{}.json".format(tag, sid)), "w"
        ) as outfile:
            json.dump(strat_output, outfile)
        ## call the function to update the main progress bar
        updater()

def find_best_strat_from_json(tag):
    best_score = 0
    best_strat = None
    file_list = os.listdir(OUTPUT_DIR)
    for filename in file_list:
        if not "strategy_{}_".format(tag) in filename:
            continue
        full_filename = os.path.join(OUTPUT_DIR, filename)
        with open(full_filename) as strat_file:
            strat = json.load(strat_file)
            if strat["total_score"] > best_score:
                best_score = strat["total_score"]
                best_strat = strat
        ## cleanup
        os.remove(full_filename)
    return best_strat


def print_strat(strat):
    pass


def main():

    parser = argparse.ArgumentParser(
        description="Try some different transfer strategies"
    )
    parser.add_argument(
        "--weeks_ahead", help="how many weeks ahead", type=int, default=3
    )
    parser.add_argument("--tag", help="specify a string identifying prediction set")
    parser.add_argument(
        "--num_iterations", help="how many trials to run", type=int, default=100
    )
    parser.add_argument("--exhaustive_double_transfer",
                        help="use exhaustive search when doing 2 transfers in gameweek",
                        action="store_true")
    parser.add_argument("--allow_wildcard",
                        help="include possibility of wildcarding in one of the weeks",
                        action="store_true")
    parser.add_argument("--max_points_hit",
                        help="how many points are we prepared to lose on transfers",
                        type=int, default=4)
    parser.add_argument("--num_free_transfers",
                        help="how many free transfers do we have",
                        type=int, default=1)
    parser.add_argument("--bank",
                        help="how much money do we have in the bank (multiplied by 10)?",
                        type=int, default=0)
    parser.add_argument("--num_thread",
                        help="how many threads to use",
                        type=int, default=4)
    parser.add_argument("--season",
                        help="what season, in format e.g. '1819'",
                        type=int, default=CURRENT_SEASON)
    args = parser.parse_args()
    season = args.season
    num_weeks_ahead = args.weeks_ahead
    num_iterations = args.num_iterations
    exhaustive_double_transfer = args.exhaustive_double_transfer
    if args.allow_wildcard:
        wildcard = True
    else:
        wildcard = False
    num_free_transfers = args.num_free_transfers
    budget =  args.bank
    max_points_hit = args.max_points_hit
    if args.tag:
        tag = args.tag
    else:
        ## get most recent set of predictions from DB table
        tag = get_latest_prediction_tag()

    ## create the output directory for temporary json files
    ## giving the points prediction for each strategy
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if len(os.listdir(OUTPUT_DIR)) > 0:
        os.system("rm "+OUTPUT_DIR+"/*")

    ## first get a baseline prediction
    baseline_score, baseline_dict = get_baseline_prediction(num_weeks_ahead, tag)

    ## create a queue that we will add strategies to, and some processes to take
    ## things off it
    squeue = Queue()
    procs = []
    ## create one progress bar for each thread
    progress_bars = []
    for i in range(args.num_thread):
        progress_bars.append(tqdm(total=100))
    ### generate the list of transfer strategies
    strategies = generate_transfer_strategies(num_weeks_ahead,
                                              num_free_transfers, max_points_hit,
                                              wildcard)
    ## define overall progress bar
    total_progress = tqdm(total=len(strategies), desc="Total progress")

    ## functions to be passed to subprocess to update or reset progress bars
    def reset_progress(index, strategy_string):
        if strategy_string == "DONE":
            progress_bars[index].close()
        else:
            progress_bars[index].n=0
            progress_bars[index].desc="strategy: "+strategy_string
            progress_bars[index].refresh()
    def update_progress(increment=1, index=None):
        if index==None:
            ## outer progress bar
            nfiles = len(os.listdir(OUTPUT_DIR))
            total_progress.n = nfiles
            total_progress.refresh()
            if nfiles == len(strategies):
                total_progress.close()
                for pb in progress_bars:
                    pb.close()
        else:
            progress_bars[index].update(increment)
            progress_bars[index].refresh()
    for i in range(args.num_thread):
        processor = Process(
            target=process_strat,
            args=(squeue, i, num_iterations, exhaustive_double_transfer,
                  tag, baseline_dict, update_progress, reset_progress, budget),
        )
        processor.daemon = True
        processor.start()
        procs.append(processor)

    ## add the strategies to the queue
    for strat in strategies:
        squeue.put(strat)
    for i in range(args.num_thread):
        squeue.put("DONE")
    ### now rejoin the main thread
    for i,p in enumerate(procs):
        progress_bars[i].close()
        progress_bars[i] = None
        p.join()

    ### find the best from all the strategies tried
    best_strategy = find_best_strat_from_json(tag)

    fill_suggestion_table(baseline_score, best_strategy, season)
    for i in range(len(procs)):
        print("\n")
    print("\n====================================\n")
    print("Baseline score: {}".format(baseline_score))
    print("Best score: {}".format(best_strategy["total_score"]))
    print(" best strategy")
    print(best_strategy)
