#!/usr/bin/env python3
'''
Preprocessing script running the preprocessing based on configuration
options.
'''
# standard libraries
import os
import logging
import logging.config
from typing import Tuple, List

import pickle
from argparse import ArgumentParser

# own libraries
from lib import download, errorfixer
from lib.visual import progress_bar
from lib.model import json, case, config
from lib.api import phenomizer, omim, mutalyzer


def configure_logging(logger_name, logger_file: str = "preprocess.log"):
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    # visible screen printing
    stdout_channel = logging.StreamHandler()
    stdout_channel.setLevel(logging.INFO)
    # file output logging
    file_channel = logging.FileHandler(logger_file, mode="w")
    file_channel.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(message)s")
    stdout_channel.setFormatter(formatter)

    file_formatter = logging.Formatter(
        "%(asctime)s L%(lineno)d <%(module)s|%(funcName)s> %(message)s"
    )
    file_channel.setFormatter(file_formatter)

    logger.addHandler(stdout_channel)
    logger.addHandler(file_channel)


def parse_arguments():
    parser = ArgumentParser(description=(
        "Process f2g provided jsons into a format processable by "
        "classification."))
    parser.add_argument("-s", "--single", help="Process a single json file.")
    parser.add_argument(
        "-o", "--output",
        help="Destination of created old json.",
        default=""
    )
    parser.add_argument(
        "-p", "--pickle",
        help="Start with pickled cases after phenomization."
    )
    parser.add_argument(
        "-e", "--entry",
        help=("Start entrypoint for pickled results. "
              "Default: pheno - start at phenomization"
              "Used in conjunction with --pickle"),
        default="pheno"
    )

    return parser.parse_args()


def json_from_directory(config_data: config.ConfigManager) \
        -> Tuple[List[str], str]:
    '''Get a list of json file paths.'''
    # Download new files from AWS Bucket
    if config_data.general["download"]:
        download.backup_s3_folder(config=config_data)

    # Initial Quality check of new json
    unprocessed_jsons = os.path.join(
        config_data.aws['download_location'], 'cases')
    json_files = [os.path.join(unprocessed_jsons, x)
                  for x in os.listdir(unprocessed_jsons)
                  if os.path.splitext(x)[1] == '.json']
    # corrected is a directory which can contain manually edited case jsons
    # that should differ from the original only in content, not in overall
    # structure.
    # this should make resolving some exotic errors a lot easier
    corrected = config_data.preprocess['corrected_location']

    return json_files, corrected


@progress_bar("Process jsons")
def yield_jsons(json_files, corrected):
    for json_file in json_files:
        yield json.NewJson.from_file(json_file, corrected)


@progress_bar("Create cases")
def yield_cases(json_files, error_fixer, exclusion):
    for json_file in json_files:
        yield case.Case(
            json_file,
            error_fixer=error_fixer,
            exclude_benign_variants=exclusion
        )


@progress_bar("Phenomization")
def yield_phenomized(case_objs, phen):
    for case_obj in case_objs:
        case_obj.phenomize(phen)
        yield


@progress_bar("Convert old")
def yield_old_json(case_objs, destination, omim_obj):
    for case_obj in case_objs:
        old = json.OldJson.from_case_object(case_obj, destination, omim_obj)
        old.save_json()
        yield


def create_jsons(args, config_data):
    # get either from single file or from directory
    json_files, corrected = ([args.single], "") \
        if args.single else json_from_directory(config_data)
    new_json_objs = yield_jsons(json_files, corrected)

    print('Unfiltered', len(new_json_objs))

    filtered_new = [j for j in new_json_objs if j.check()[0]]
    print('Filtered rough criteria', len(filtered_new))
    return filtered_new


def create_cases(args, config_data, jsons):
    error_fixer = errorfixer.ErrorFixer(config=config_data)
    case_objs = yield_cases(
        jsons,
        error_fixer,
        config_data.preprocess["exclude_normal_variants"]
    )

    # FIXME include all cases regardless of quality
    # case_objs = [c for c in case_objs if c.check()[0]]
    # print('Cases with created hgvs objects', len(case_objs))

    mutalyzer.correct_reference_transcripts(case_objs)

    if config_data.general['dump_intermediate']:
        pickle.dump(case_objs, open('case_cleaned.p', 'wb'))

    return case_objs


def phenomize(config_data, cases):
    phen = phenomizer.PhenomizerService(config=config_data)
    yield_phenomized(cases, phen)

    if config_data.general['dump_intermediate']:
        pickle.dump(cases, open('case_phenomized.p', 'wb'))
    return cases


def convert_to_old_format(args, config_data, cases):
    destination = args.output or config_data.conversion["output_path"]

    omim_obj = omim.Omim(config=config_data)
    yield_old_json(cases, destination, omim_obj)


def main():
    '''
    Some program blocks are enabled and disabled via config options in general
    '''

    configure_logging("lib")
    config_data = config.ConfigManager()

    # Load configuration and initialize API bindings
    args = parse_arguments()
    if not args.pickle:
        jsons = create_jsons(args, config_data)
        cases = create_cases(args, config_data, jsons)
    else:
        with open(args.pickle, "rb") as pickled_file:
            cases = pickle.load(pickled_file)

    if args.entry == "pheno":
        cases = phenomize(config_data, cases)

    convert_to_old_format(args, config_data, cases)


if __name__ == '__main__':
    main()
